"""기록실 HTML → pandas DataFrame.

- 숫자 표는 pd.read_html로 읽는다.
- 선수 이름 링크(…?playerId=62404)에서 playerId를 뽑아 선수 식별 키로 쓴다(동명이인 구분).
- 열 이름은 화면 머리글의 영문 약어(AVG, G, 2B …)를 소문자로 바꿔 쓴다.
"""

import re
from io import StringIO

import pandas as pd
from lxml import html as lxml_html

TABLE_XPATH = (
    "//table[contains(concat(' ', normalize-space(@class), ' '), ' tData01 ')"
    " or contains(concat(' ', normalize-space(@class), ' '), ' tData ')]"
)
PLAYER_ID_RE = re.compile(r"playerId=(\d+)")

# 한글 머리글 → 열 이름 (팀 순위 표는 머리글이 한글이다)
KOREAN_HEADERS = {
    "순위": "rank",
    "선수명": "player_name",
    "팀명": "team_name",
    "경기": "g",
    "승": "w",
    "패": "l",
    "무": "t",
    "승률": "wpct",
    "게임차": "gb",
    "최근10경기": "last10",
    "연속": "streak",
    "홈": "home",
    "방문": "away",
}
# SQL에서 숫자로 시작하는 열 이름은 불편하므로 바꿔 준다
RENAME = {"2b": "doubles", "3b": "triples"}


def _column_names(table) -> list[str]:
    names = []
    for th in table.xpath("./thead/tr/th"):
        link = th.xpath("./a")
        # 정렬 링크가 있으면 링크 글자(AVG, G …)만 쓴다. 툴팁 글자가 섞여 들어오는 것을 막는다.
        text = (link[0].text_content() if link else th.text_content()).strip()
        name = KOREAN_HEADERS.get(text, text.lower())
        name = RENAME.get(name, name).replace("%", "_pct")
        names.append(re.sub(r"[^0-9a-z_]+", "_", name) if name.isascii() else name)
    return names


def parse_record_table(page_html: str, with_player_id: bool = True) -> pd.DataFrame:
    """페이지 HTML 하나에서 기록 표를 DataFrame으로 만든다. 표가 비어 있으면 빈 DataFrame.

    with_player_id=False 이면 팀 기록처럼 선수 링크가 없는 표로 보고 그대로 읽는다.
    """
    doc = lxml_html.fromstring(page_html)
    tables = doc.xpath(TABLE_XPATH)
    if not tables:
        raise ValueError("기록 표(table.tData01)를 찾지 못했습니다")
    table = tables[0]
    # 머리글에 마우스가 올라가면 화면에 '경기' 같은 툴팁 div가 끼어든다. 파싱 전에 지운다.
    for tip in table.xpath(".//*[contains(@class, 'tooltip')]"):
        tip.drop_tree()

    if not with_player_id:
        return _parse_team_tables(tables)

    player_ids = []
    for row in table.xpath("./tbody/tr"):
        hrefs = row.xpath(".//a[contains(@href, 'playerId=')]/@href")
        if hrefs:
            player_ids.append(PLAYER_ID_RE.search(hrefs[0]).group(1))
    if not player_ids:
        return pd.DataFrame()

    table_html = lxml_html.tostring(table, encoding="unicode")
    df = pd.read_html(StringIO(table_html), flavor="lxml")[0]
    df.columns = _column_names(table)
    if len(df) != len(player_ids):
        raise ValueError(f"행 수({len(df)})와 playerId 수({len(player_ids)})가 다릅니다")

    df.insert(0, "player_id", [int(p) for p in player_ids])
    return _clean_values(df)


def _parse_team_tables(tables) -> pd.DataFrame:
    """팀 기록·순위 표. 2000년처럼 리그가 둘(드림/매직)이면 순위표가 여러 개라서 모두 읽는다.

    '순위'로 시작하는 표만 쓴다(팀 순위 화면 아래의 팀 간 상대전적 표는 건너뛴다).
    """
    frames = []
    for i, table in enumerate(tables):
        headers = [th.text_content().strip() for th in table.xpath("./thead/tr/th")]
        if not headers or headers[0] != "순위" or not table.xpath("./tbody/tr/td"):
            continue
        df = pd.read_html(StringIO(lxml_html.tostring(table, encoding="unicode")), flavor="lxml")[0]
        df.columns = _column_names(table)
        df = df[df["team_name"].notna()]
        df["table_no"] = i + 1  # 같은 화면의 몇 번째 표인지 (리그가 둘인 해 구분용)
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return _clean_values(pd.concat(frames, ignore_index=True))


def _clean_values(df: pd.DataFrame) -> pd.DataFrame:
    """타수 0인 선수의 타율처럼 '-'로 표시된 값은 빈 값(NULL)으로 바꾸고, 숫자 열은 숫자로 만든다.

    '147 1/3' 같은 이닝(ip)은 숫자로 바뀌지 않으므로 글자 그대로 둔다.
    """
    df = df.replace("-", pd.NA)
    for col in df.columns:
        if col in ("player_name", "team_name", "last10", "streak", "home", "away") or pd.api.types.is_numeric_dtype(df[col]):
            continue
        converted = pd.to_numeric(df[col], errors="coerce")
        if converted.notna().sum() == df[col].notna().sum():
            df[col] = converted
    return df


def parse_pages(
    pages_html: list[str],
    key_cols: tuple[str, ...] = ("player_id",),
    with_player_id: bool = True,
) -> pd.DataFrame:
    """여러 페이지를 합치고, 같은 키(선수, 수비는 선수+포지션)가 두 번 나오면 오류를 낸다."""
    frames = [parse_record_table(h, with_player_id) for h in pages_html]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    # 같은 경기수끼리는 순서가 매번 달라서, 페이지 경계에서 같은 줄이 두 번 나오고 다른 줄이 빠질 수 있다.
    # 완전히 같은 줄은 하나만 남기고, 몇 줄 지웠는지 attrs에 적어 둔다(수집기가 보고 한 번 더 받는다).
    before = len(df)
    # 순위(rank)는 정렬 방향에 따라 달라지므로 비교에서 뺀다
    df = df.drop_duplicates(subset=[c for c in df.columns if c != "rank"], ignore_index=True)
    df.attrs["dropped_duplicates"] = before - len(df)
    dup = df[df.duplicated(list(key_cols), keep=False)]
    if not dup.empty:
        raise ValueError(f"중복 키 {key_cols} 발견:\n{dup.head(10)}")
    return df
