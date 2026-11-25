from datetime import date

from kbo_ai_platform.ingestion.game_logs import SPECS, ip_to_outs, parse_game_log

PITCHER_HTML = """<html><body>
<table class="tbl tt mb5"><thead><tr><th>5월</th><th>상대</th><th>구분</th><th>결과</th><th>ERA1</th>
<th>TBF</th><th>IP</th><th>H</th><th>HR</th><th>BB</th><th>HBP</th><th>SO</th><th>R</th><th>ER</th><th>ERA2</th></tr></thead>
<tfoot><tr><th colspan="2">합계</th><th>-</th></tr></tfoot>
<tbody>
<tr><td>05.01</td><td>LG</td><td>구원</td><td>홀</td><td>0.00</td><td>4</td><td>1 1/3</td><td>0</td><td>0</td><td>1</td><td>0</td><td>2</td><td>0</td><td>0</td><td>2.10</td></tr>
<tr><td>05.01</td><td>LG</td><td>구원</td><td></td><td>-</td><td>1</td><td>0</td><td>1</td><td>0</td><td>0</td><td>0</td><td>0</td><td>1</td><td>1</td><td>2.50</td></tr>
</tbody></table></body></html>"""


def test_ip_to_outs():
    assert ip_to_outs("5 2/3") == 17
    assert ip_to_outs("1/3") == 1
    assert ip_to_outs("7") == 21
    assert ip_to_outs("") is None


def test_parse_pitcher_doubleheader():
    rows = parse_game_log(PITCHER_HTML, SPECS["pitcher"], 2024, 12345)
    assert [r["game_no"] for r in rows] == [1, 2]
    assert rows[0]["game_date"] == date(2024, 5, 1)
    assert rows[0]["decision"] == "홀" and rows[1]["decision"] is None
    assert rows[0]["ip_outs"] == 4 and rows[1]["ip_outs"] == 0
    assert rows[1]["game_era"] is None and rows[1]["season_era"] == 2.5
