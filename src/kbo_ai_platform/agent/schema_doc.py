"""LLM 에게 보여 줄 DB 설명서.

열 목록은 DB(information_schema)에서 그대로 읽어 오고, 사람이 쓴 설명(TABLE_NOTES, GLOBAL_NOTES)을 붙인다.
그래서 테이블에 열이 추가돼도 설명서가 자동으로 따라간다.
"""

from functools import lru_cache

from sqlalchemy import Engine, text

# 에이전트가 조회해도 되는 테이블과 설명. 여기에 없는 테이블은 설명서에도, SQL 허용 목록에도 들어가지 않는다.
TABLE_NOTES: dict[str, str] = {
    "analytics.hitter_season": (
        "타자 시즌 기록 (1행 = 선수 1명 × 시즌). 기본·추가·주루 기록을 합치고 세이버 지표를 계산한 뷰. "
        "타자 질문은 raw 대신 이 뷰를 먼저 쓴다. avg 타율, obp 출루율, slg 장타율, ops, iso 순수장타율, "
        "babip, bb_rate 볼넷률, k_rate 삼진률(타석 대비), risp 득점권타율, ph_ba 대타타율, mh 멀티히트, "
        "sba 도루시도, sb 도루, cs 도루실패, gdp 병살타, ubb 고의4구 제외 볼넷."
    ),
    "analytics.pitcher_season": (
        "투수 시즌 기록 (1행 = 선수 1명 × 시즌). era 평균자책점, whip, fip(리그 상수 적용), k9·bb9·hr9, "
        "k_rate·bb_rate(상대타자 대비), babip(피BABIP), opp_avg 피안타율, qs 퀄리티스타트, hld 홀드, sv 세이브, "
        "bsv 블론세이브, np 투구수, tbf 상대타자수. 이닝은 ip(글자, 예: '147 1/3') 대신 ip_decimal(숫자) 또는 outs 를 쓴다."
    ),
    "analytics.team_season": (
        "팀 시즌 순위·성적 (1행 = 팀 × 시즌). rank 순위, w 승, l 패, t 무, wpct 승률, gb 게임차, runs 득점, "
        "runs_allowed 실점, run_diff 득실차, pyth_wpct 피타고라스 기대승률(지수 2), pyth_wpct_183(지수 1.83), "
        "pyth_wins 기대승수. 2000년은 드림·매직 양대리그라 table_no 1·2 로 순위표가 나뉜다(그 해 rank 는 리그 안 순위)."
    ),
    "analytics.league_pitching": "시즌별 리그 전체 투수 합계와 리그 ERA, FIP 상수.",
    "raw.defense_basic": (
        "선수 수비 기록 (선수 × 시즌 × 포지션 pos: 투수, 포수, 1루수, 2루수, 3루수, 유격수, 좌익수, 중견수, 우익수). "
        "gs 선발, ip 수비이닝(글자), e 실책, po 자살, a 보살, dp 병살, fpct 수비율, pb 포일, sb·cs·cs_pct 도루허용·저지·저지율(포수)."
    ),
    "raw.player_profile": (
        "선수 프로필 (1행 = 선수, player_id 로 기록 테이블과 연결). team_name 은 '롯데 자이언츠' 처럼 정식 팀명(현재/마지막 소속)이라 "
        "기록 테이블의 짧은 팀명과 다르다. position(투수/포수/내야수/외야수), throws_bats(예: 우투좌타), height_cm, weight_kg, "
        "birthday, career 출신학교, draft 지명 정보, join_year 입단연도, salary_manwon 올해 연봉(만원), signing_bonus_text 계약금."
    ),
    "raw.fa_contracts": (
        "FA 계약 (1행 = fa_year × 선수, 나무위키 기준 주요 계약 361건, 모든 FA 가 있지는 않음). fa_year 는 계약이 적용되는 첫 시즌. "
        "fa_type: FA/퓨처스FA/해외, move_type: 잔류/이적/해외. 금액 단위는 원(KRW): total_krw 총액, signing_bonus_krw 계약금, "
        "annual_salary_krw 연봉, option_krw 옵션. contract_years 계약 연수. orig_team → new_team. "
        "kbo_player_id 는 TEXT 라서 기록 테이블과 연결할 때 kbo_player_id::bigint = player_id 로 바꾼다(없을 수 있음)."
    ),
    "raw.fx_rates_yearly": "연평균 환율 (currency: USD, JPY100(100엔당)), krw_per_unit = 1단위당 원화. is_partial_year 는 올해처럼 1년이 안 된 값.",
    "raw.fx_rates_monthly": "월평균 환율 (year, month, currency, krw_per_unit).",
    "raw.team_hitter": "팀 타격 기록 (팀 × 시즌). 2000년은 비어 있다.",
    "raw.team_pitcher": "팀 투수 기록 (팀 × 시즌). era, whip 등. 2000년은 비어 있다.",
    "raw.team_defense": "팀 수비 기록 (팀 × 시즌).",
    "raw.team_runner": "팀 주루 기록 (팀 × 시즌).",
    "raw.team_rank": "팀 순위표 원본. last10 최근10경기, streak 연속, home·away 홈/원정 성적(글자). 분석엔 analytics.team_season 이 더 편하다.",
}

GLOBAL_NOTES = """\
- 데이터 범위: KBO 정규시즌 2000~2026. 모든 기록 테이블의 series 는 '0'(정규시즌) 하나뿐이다.
- 선수는 player_id 로 구분한다. 동명이인이 많으니(이름이 겹치는 경우 171개) 이름으로 찾은 뒤 결과에 player_id·팀·시즌을 함께 보여 준다.
- 선수 시즌 기록은 시즌당 1행이다. 시즌 중 이적해도 팀별로 나뉘지 않는다(team_name 은 기록실 표기 소속).
- 기록 테이블의 team_name 은 당시 짧은 팀명이다: 롯데, 한화, LG, 두산, 삼성, KIA, NC, KT, 키움, SSG.
  옛 이름: 해태(2000) → KIA(2001~), SK(2000~2020) → SSG(2021~), 현대(2000~2007), 우리(2008) → 히어로즈(2009) → 넥센(2010~2018) → 키움(2019~).
  사용자가 'SSG' 의 역대 기록을 물으면 SK 시절을 포함할지 답변에 밝힌다.
- 규정타석(팀 경기수 × 3.1)·규정이닝(팀 경기수 × 1.0) 정보는 따로 없다. "타율 1위" 같은 순위 질문은
  타자는 pa >= 경기수×3.1, 투수는 ip_decimal >= 경기수 로 거른다. 그 시즌 팀 경기수는 analytics.team_season 의 max(g) 로 구한다.
- raw.team_hitter, raw.team_pitcher, raw.team_defense, raw.team_runner 에는 리그 전체 합계 행(team_name = '합계')이 섞여 있다. 팀 비교·순위엔 team_name <> '합계' 로 뺀다.
- 비율 지표(avg, era 등)로 정렬할 땐 NULL 이 위로 오지 않게 NULLS LAST 를 쓴다.
"""


@lru_cache(maxsize=1)
def _columns(engine: Engine) -> dict[str, list[tuple[str, str]]]:
    sql = text(
        """
        SELECT table_schema || '.' || table_name AS t, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema IN ('raw', 'analytics')
        ORDER BY t, ordinal_position
        """
    )
    out: dict[str, list[tuple[str, str]]] = {}
    with engine.connect() as conn:
        for t, col, dtype in conn.execute(sql):
            if t in TABLE_NOTES and col not in ("scraped_at", "source_url", "source_section"):
                out.setdefault(t, []).append((col, _short_type(dtype)))
    return out


def _short_type(dtype: str) -> str:
    return {
        "character varying": "text",
        "double precision": "float",
        "timestamp with time zone": "timestamptz",
        "timestamp without time zone": "timestamp",
    }.get(dtype, dtype)


def allowed_tables() -> set[str]:
    return set(TABLE_NOTES)


def build_schema_doc(engine: Engine) -> str:
    cols = _columns(engine)
    parts = ["# 테이블 (PostgreSQL)"]
    for table, note in TABLE_NOTES.items():
        if table not in cols:
            continue
        col_text = ", ".join(f"{c} {t}" for c, t in cols[table])
        parts.append(f"## {table}\n{note}\n열: {col_text}")
    parts.append("# 데이터 규칙\n" + GLOBAL_NOTES)
    return "\n\n".join(parts)
