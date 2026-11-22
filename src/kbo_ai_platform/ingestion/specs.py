"""수집할 기록실 화면 목록.

각 항목은 '어느 화면을, 어떤 정렬로 열어서, 어느 테이블에, 무엇을 키로 넣을지'를 정한다.
sort_col: 타율·평균자책점 같은 비율 기록으로 정렬하면 규정타석/이닝 선수만 나오므로,
          경기수처럼 누적 기록으로 정렬해 출전 선수 전원을 받는다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RecordSpec:
    path: str
    table: str
    sort_col: str | None = None
    key_cols: tuple[str, ...] = ("player_id",)
    with_player_id: bool = True
    # 이 화면을 주소로 바로 열면 다른 화면으로 튕기는 경우, entry_path 에서 시즌을 고른 뒤
    # '다음' 버튼으로 넘어간다 (타자 기본기록 2페이지가 그렇다)
    entry_path: str | None = None


PLAYER_SPECS = {
    # 타율 경기 타석 타수 득점 안타 2루타 3루타 홈런 루타 타점 희생번트 희생플라이
    "hitter": RecordSpec("/Record/Player/HitterBasic/Basic1.aspx", "hitter_basic", "GAME_CN"),
    # 볼넷 고의4구 사구 삼진 병살타 장타율 출루율 OPS 멀티히트 득점권타율 대타타율
    # Basic2.aspx 를 바로 열면 옛 화면(BasicOld)으로 튕기므로 Basic1에서 시즌을 고르고 '다음'으로 들어간다.
    "hitter_extra": RecordSpec(
        "/Record/Player/HitterBasic/Basic2.aspx",
        "hitter_extra",
        "KK_CN",
        entry_path="/Record/Player/HitterBasic/Basic1.aspx",
    ),
    # 평균자책점 경기 승 패 세이브 홀드 승률 이닝 피안타 피홈런 볼넷 사구 삼진 실점 자책점 WHIP
    "pitcher": RecordSpec("/Record/Player/PitcherBasic/Basic1.aspx", "pitcher_basic", "GAME_CN"),
    # 완투 완봉 QS 블론 상대타자수 투구수 피안타율 2루타 3루타 희생번트 희생플라이 고의4구 폭투 보크
    "pitcher_extra": RecordSpec("/Record/Player/PitcherBasic/Basic2.aspx", "pitcher_extra", "PA_CN"),
    # 도루시도 도루 도루실패 도루성공률 주루사 견제사
    "runner": RecordSpec("/Record/Player/Runner/Basic.aspx", "runner_basic", "GAME_CN"),
    # 포지션별: 경기 선발 수비이닝 실책 견제사 자살 보살 병살 수비율 포일 도루허용 도루저지 저지율
    "defense": RecordSpec(
        "/Record/Player/Defense/Basic.aspx", "defense_basic", "GAME_CN", key_cols=("player_id", "pos")
    ),
}

TEAM_KEY = ("team_name",)
TEAM_SPECS = {
    "team_rank": RecordSpec("/Record/TeamRank/TeamRank.aspx", "team_rank", None, TEAM_KEY, False),
    "team_hitter": RecordSpec("/Record/Team/Hitter/Basic1.aspx", "team_hitter", None, TEAM_KEY, False),
    "team_pitcher": RecordSpec("/Record/Team/Pitcher/Basic1.aspx", "team_pitcher", None, TEAM_KEY, False),
    "team_defense": RecordSpec("/Record/Team/Defense/Basic.aspx", "team_defense", None, TEAM_KEY, False),
    "team_runner": RecordSpec("/Record/Team/Runner/Basic.aspx", "team_runner", None, TEAM_KEY, False),
}

RECORD_SPECS = {**PLAYER_SPECS, **TEAM_SPECS}

# --type 에 쓸 수 있는 묶음 이름
GROUPS = {
    "player": list(PLAYER_SPECS),
    "team": list(TEAM_SPECS),
    "all": list(RECORD_SPECS),
}
