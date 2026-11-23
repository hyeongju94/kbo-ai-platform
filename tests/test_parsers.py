from kbo_ai_platform.ingestion.parsers import parse_pages, parse_record_table

PAGE = """
<html><body><table class="tData01 tt">
<thead><tr>
  <th>순위</th><th>선수명</th><th>팀명</th>
  <th><a href="javascript:sort('HRA_RT');" title="타율">AVG</a></th>
  <th><a href="javascript:sort('GAME_CN');" title="경기">G<div class="tooltip">경기</div></a></th>
  <th><a href="javascript:sort('H2_CN');" title="2루타">2B</a></th>
</tr></thead>
<tbody>
  <tr><td>1</td><td><a href="/Record/Player/HitterDetail/Basic.aspx?playerId=53554">김민석</a></td><td>두산</td><td>0.310</td><td>135</td><td>22</td></tr>
  <tr><td>2</td><td><a href="/Record/Player/HitterDetail/Basic.aspx?playerId=54097">김민석</a></td><td>KT</td><td>-</td><td>2</td><td>0</td></tr>
</tbody></table></body></html>
"""


def test_parse_record_table_keeps_homonyms_apart():
    df = parse_record_table(PAGE)
    assert list(df.columns) == ["player_id", "rank", "player_name", "team_name", "avg", "g", "doubles"]
    assert df["player_id"].tolist() == [53554, 54097]
    assert df["avg"].iloc[0] == 0.31
    assert df["avg"].isna().iloc[1]  # '-' → NULL


def test_parse_pages_drops_rows_repeated_across_pages():
    df = parse_pages([PAGE, PAGE.replace("<td>1</td>", "<td>9</td>")])
    assert len(df) == 2
    assert df.attrs["dropped_duplicates"] == 2


def test_parse_pages_rejects_conflicting_rows_for_same_player():
    import pytest

    with pytest.raises(ValueError):
        parse_pages([PAGE, PAGE.replace("<td>135</td>", "<td>136</td>")])
