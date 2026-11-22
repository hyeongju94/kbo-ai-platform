-- 세이버메트릭스 계산용 뷰 (raw 테이블 → analytics 스키마)
-- 다시 실행해도 되도록 CREATE OR REPLACE 를 쓴다. 실행: uv run kbo-views

-- 이닝 글자("147 1/3", "2/3", "161")를 아웃카운트 수로 바꾼다
CREATE OR REPLACE FUNCTION analytics.ip_to_outs(ip TEXT) RETURNS INTEGER
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN ip IS NULL OR btrim(ip) = '' THEN NULL
        WHEN ip ~ '^\d+ \d/3$' THEN split_part(ip, ' ', 1)::int * 3 + left(split_part(ip, ' ', 2), 1)::int
        WHEN ip ~ '^\d/3$' THEN left(ip, 1)::int
        WHEN ip ~ '^\d+$' THEN ip::int * 3
    END
$$;

-- 타자: 기본기록 + 추가기록(볼넷·삼진·출루율…) + 주루
CREATE OR REPLACE VIEW analytics.hitter_season AS
SELECT
    b.season, b.series, b.player_id, b.player_name, b.team_name,
    b.g, b.pa, b.ab, b.r, b.h,
    b.h - b.doubles - b.triples - b.hr AS singles,
    b.doubles, b.triples, b.hr, b.tb, b.rbi, b.sac, b.sf,
    x.bb, x.ibb, x.bb - x.ibb AS ubb, x.hbp, x.so, x.gdp, x.mh,
    ru.sba, ru.sb, ru.cs, ru.oob, ru.pko,
    b.avg, x.obp, x.slg, x.ops, x.risp, x.ph_ba,
    round((b.tb - b.h)::numeric / nullif(b.ab, 0), 3) AS iso,
    -- BABIP = (안타 - 홈런) / (타수 - 삼진 - 홈런 + 희생플라이)
    round((b.h - b.hr)::numeric / nullif(b.ab - x.so - b.hr + b.sf, 0), 3) AS babip,
    round(x.bb::numeric / nullif(b.pa, 0), 3) AS bb_rate,
    round(x.so::numeric / nullif(b.pa, 0), 3) AS k_rate
FROM raw.hitter_basic b
LEFT JOIN raw.hitter_extra x USING (season, series, player_id)
LEFT JOIN raw.runner_basic ru USING (season, series, player_id);

-- 리그(시즌) 전체 투수 합계: FIP 상수 계산용
CREATE OR REPLACE VIEW analytics.league_pitching AS
SELECT
    p.season, p.series,
    sum(analytics.ip_to_outs(p.ip)) AS outs,
    sum(p.er) AS er, sum(p.r) AS r, sum(p.hr) AS hr, sum(p.bb) AS bb,
    sum(p.hbp) AS hbp, sum(p.so) AS so, sum(p.h) AS h,
    round(sum(p.er) * 27.0 / nullif(sum(analytics.ip_to_outs(p.ip)), 0), 3) AS era,
    -- FIP 상수 = 리그 ERA - (13*HR + 3*(BB+HBP) - 2*SO) / 리그 이닝
    round(sum(p.er) * 27.0 / nullif(sum(analytics.ip_to_outs(p.ip)), 0)
          - (13 * sum(p.hr) + 3 * (sum(p.bb) + sum(p.hbp)) - 2 * sum(p.so)) * 3.0
            / nullif(sum(analytics.ip_to_outs(p.ip)), 0), 3) AS fip_constant
FROM raw.pitcher_basic p
GROUP BY p.season, p.series;

-- 투수: 기본기록 + 추가기록 + FIP·BABIP 등
CREATE OR REPLACE VIEW analytics.pitcher_season AS
SELECT
    p.season, p.series, p.player_id, p.player_name, p.team_name,
    p.g, p.w, p.l, p.sv, p.hld, p.ip,
    analytics.ip_to_outs(p.ip) AS outs,
    round(analytics.ip_to_outs(p.ip) / 3.0, 3) AS ip_decimal,
    p.h, p.hr, p.bb, p.hbp, p.so, p.r, p.er,
    e.cg, e.sho, e.qs, e.bsv, e.tbf, e.np, e.doubles, e.triples, e.sac, e.sf, e.ibb, e.wp, e.bk,
    p.era, p.whip, e.avg AS opp_avg,
    round((13 * p.hr + 3 * (p.bb + p.hbp) - 2 * p.so) * 3.0
          / nullif(analytics.ip_to_outs(p.ip), 0) + lg.fip_constant, 2) AS fip,
    round(p.so * 27.0 / nullif(analytics.ip_to_outs(p.ip), 0), 2) AS k9,
    round(p.bb * 27.0 / nullif(analytics.ip_to_outs(p.ip), 0), 2) AS bb9,
    round(p.hr * 27.0 / nullif(analytics.ip_to_outs(p.ip), 0), 2) AS hr9,
    round(p.so::numeric / nullif(e.tbf, 0), 3) AS k_rate,
    round(p.bb::numeric / nullif(e.tbf, 0), 3) AS bb_rate,
    -- 피BABIP = (피안타 - 피홈런) / (타수 - 삼진 - 피홈런 + 희생플라이), 타수 = 상대타자 - 볼넷 - 사구 - 희생번트 - 희생플라이
    round((p.h - p.hr)::numeric
          / nullif((e.tbf - p.bb - p.hbp - e.sac - e.sf) - p.so - p.hr + e.sf, 0), 3) AS babip
FROM raw.pitcher_basic p
LEFT JOIN raw.pitcher_extra e USING (season, series, player_id)
LEFT JOIN analytics.league_pitching lg USING (season, series);

-- 팀: 순위 + 득점·실점 + 피타고라스 승률
-- 팀 타격/투수 기록이 사이트에 없는 해(2000년)는 선수 기록을 팀별로 더해 득점·실점을 채운다.
CREATE OR REPLACE VIEW analytics.team_season AS
WITH player_runs AS (
    SELECT season, series, team_name, sum(r) AS r FROM raw.hitter_basic GROUP BY 1, 2, 3
), player_runs_allowed AS (
    SELECT season, series, team_name, sum(r) AS ra FROM raw.pitcher_basic GROUP BY 1, 2, 3
), base AS (
    SELECT
        tr.season, tr.series, tr.table_no, tr.rank, tr.team_name,
        tr.g, tr.w, tr.l, tr.t, tr.wpct, tr.gb,
        coalesce(th.r, pr.r) AS runs,
        coalesce(tp.r, pra.ra) AS runs_allowed,
        (th.r IS NULL) AS runs_from_players
    FROM raw.team_rank tr
    LEFT JOIN raw.team_hitter th USING (season, series, team_name)
    LEFT JOIN raw.team_pitcher tp USING (season, series, team_name)
    LEFT JOIN player_runs pr USING (season, series, team_name)
    LEFT JOIN player_runs_allowed pra USING (season, series, team_name)
)
SELECT
    base.*,
    runs - runs_allowed AS run_diff,
    round(power(runs, 2)::numeric / nullif(power(runs, 2) + power(runs_allowed, 2), 0), 3) AS pyth_wpct,
    round(power(runs, 1.83)::numeric / nullif(power(runs, 1.83) + power(runs_allowed, 1.83), 0), 3) AS pyth_wpct_183,
    -- 피타고라스 기대승수 (무승부 제외 경기 기준)
    round((w + l) * power(runs, 2)::numeric / nullif(power(runs, 2) + power(runs_allowed, 2), 0), 1) AS pyth_wins
FROM base;
