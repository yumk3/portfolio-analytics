"""Unit tests for each of the archiver's content checks on its own, with fabricated data.

The checks overlap by design, so a regression in one can be hidden by another.
Each check here has direct tests plus a validate() case that only that check
catches, identified by its reason text or blocked flag.
"""
import importlib.util
import io
import pathlib

import openpyxl
import pandas as pd
import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("etf_archiver_checks_under_test",
                                              REPO / "archiver" / "etf_archiver.py")
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)

GOOD_CSV = (b"Ticker,Name,Identifier,Weight,Shares,Market Value\n"
            b"XYZ,Example Corp,000000001,60.00%,100,1000\n"
            b"ABC,Sample Inc,000000002,40.00%,50,700\n")


def xlsx_bytes(rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def rejected(tmp_path, content, ext, issuer="example"):
    path = tmp_path / f"XYZ_{issuer}.{ext}"
    path.write_bytes(content)
    with pytest.raises(arc.Rejected) as e:
        arc.validate(path, ext, issuer)
    return str(e.value), e.value.blocked


def accepted(tmp_path, content, ext, issuer="example"):
    path = tmp_path / f"XYZ_{issuer}.{ext}"
    path.write_bytes(content)
    df, _ = arc.validate(path, ext, issuer)
    return df


# ------------------------------------------------------------ looks_like_html
@pytest.mark.parametrize("content", [
    b"<!DOCTYPE html><html><body>x</body></html>",
    b"  \r\n<!doctype HTML>",
    b"<HTML lang='en'>",
    b"Access denied <title>Blocked</title>",
    b"Error\n<head><meta charset='utf-8'></head>",
])
def test_looks_like_html_detects_pages(content):
    assert arc.looks_like_html(content)


@pytest.mark.parametrize("content", [
    GOOD_CSV,
    b'{"holdings": [{"ticker": "XYZ"}]}',
    b"PK\x03\x04rest-of-zip",
    b"x" * 1000 + b"<title>too late</title>",        # only the first 1000 bytes count
])
def test_looks_like_html_ignores_data(content):
    assert not arc.looks_like_html(content)


def test_validate_blocks_html_that_does_not_start_with_markup(tmp_path):
    # Only looks_like_html catches this: it does not start with "<".
    reason, blocked = rejected(tmp_path, b"Access denied <title>Blocked</title>\n", "csv")
    assert blocked and "HTML page" in reason


def test_validate_blocks_html_served_as_json(tmp_path):
    reason, blocked = rejected(tmp_path, b"<html><head><title>503</title></head></html>", "json")
    assert blocked and "HTML page" in reason


# ------------------------------------------------------------ looks_like_markup
@pytest.mark.parametrize("content", [
    b"<?xml version='1.0'?><Error><Code>AccessDenied</Code></Error>",
    b"\xef\xbb\xbf<root/>",                            # UTF-8 byte-order mark first
    b"\r\n  <data>",
    b"<!DOCTYPE html>",                               # includes everything HTML catches
])
def test_looks_like_markup_detects_markup(content):
    assert arc.looks_like_markup(content)


@pytest.mark.parametrize("content", [GOOD_CSV, b"Fund,As of 01/02/2026\n<not first>"])
def test_looks_like_markup_ignores_text_not_starting_with_markup(content):
    assert not arc.looks_like_markup(content)


def test_validate_blocks_xml_error_as_csv(tmp_path):
    reason, blocked = rejected(
        tmp_path, b"<?xml version='1.0'?><Error><Code>AccessDenied</Code></Error>", "csv")
    assert blocked and "markup" in reason


def test_validate_blocks_markup_wrapped_csv(tmp_path):
    # Without the markup check this would parse as a holdings table and be filed.
    reason, blocked = rejected(tmp_path, b"<data>\n" + GOOD_CSV + b"</data>\n", "csv")
    assert blocked and "markup" in reason


# ------------------------------------------------------------ file signature (MAGIC)
def test_real_xlsx_starts_with_the_zip_signature():
    assert xlsx_bytes([["Ticker", "Weight"]]).startswith(arc.MAGIC["xlsx"])


@pytest.mark.parametrize("ext", ["xlsx", "xls"])
def test_validate_rejects_text_saved_as_excel(tmp_path, ext):
    reason, blocked = rejected(tmp_path, GOOD_CSV, ext)
    assert f"not a real .{ext} file" in reason and not blocked


def test_validate_marks_markup_saved_as_excel_blocked(tmp_path):
    reason, blocked = rejected(tmp_path, b"<?xml version='1.0'?><Error/>", "xlsx")
    assert "not a real .xlsx file" in reason and blocked


def test_validate_accepts_real_xlsx(tmp_path):
    df = accepted(tmp_path, xlsx_bytes([["Fund holdings as of 01/02/2026"],
                                        ["Ticker", "Name", "Weight"],
                                        ["XYZ", "Example Corp", 0.6]]), "xlsx")
    assert list(df["Ticker"]) == ["XYZ"]


# ------------------------------------------------------------ check_json_holdings
def test_check_json_holdings_counts_rows():
    assert arc.check_json_holdings(b'{"holdings": [{"ticker": "XYZ"}, {"ticker": "ABC"}]}') == 2


@pytest.mark.parametrize("content, message", [
    (b"not json", "not valid JSON"),
    (b'[{"ticker": "XYZ"}]', "is a list, expected an object"),
    (b'{"error": "rate limited"}', "no 'holdings' rows"),
    (b'{"holdings": []}', "no 'holdings' rows"),
    (b'{"holdings": {"ticker": "XYZ"}}', "no 'holdings' rows"),
])
def test_check_json_holdings_rejects_non_holdings(content, message):
    with pytest.raises(ValueError, match=message):
        arc.check_json_holdings(content)


@pytest.mark.parametrize("content, message", [
    (b'{"error": "rate limited"}', "no 'holdings' rows"),
    (b'{"holdings": []}', "no 'holdings' rows"),
    (b'[{"ticker": "XYZ"}]', "is a list"),
])
def test_validate_rejects_json_error_payloads(tmp_path, content, message):
    reason, blocked = rejected(tmp_path, content, "json")
    assert message in reason and not blocked


# ------------------------------------------------------------ holdings header
def test_find_header_row_skips_metadata_lines():
    lines = ["Example Fund", "Holdings as of 01/02/2026", "",
             "Ticker,Name,Weight (%),Shares", "XYZ,Example Corp,60,100"]
    assert arc.find_header_row(lines) == 3


def test_find_header_row_accepts_ticker_header_without_weight():
    assert arc.find_header_row(["Fund", '"Ticker","Name","CUSIP","Shares"']) == 1


@pytest.mark.parametrize("lines", [
    ["Notice", "This fund has closed.", "Contact us for details."],
    ["Ticker,Name"],                                   # too few columns
    ["meta"] * 60 + ["Ticker,Name,Weight %,Shares"],    # beyond the first 60 lines
])
def test_find_header_row_raises_with_preview(lines):
    with pytest.raises(ValueError, match="No header row found. First lines look like: "):
        arc.find_header_row(lines)


def test_find_excel_header_row():
    raw = pd.DataFrame([["Example Fund", None], ["Ticker", "Weight"], ["XYZ", 0.6]])
    assert arc.find_excel_header_row(raw) == 1
    with pytest.raises(ValueError, match="No header row found in the first 20 rows"):
        arc.find_excel_header_row(pd.DataFrame([["Notice"], ["Fund closed"]]))


def test_validate_rejects_csv_without_holdings_header(tmp_path):
    # Without the header check this notice would parse as a two-row table.
    reason, blocked = rejected(
        tmp_path, b"Notice\nThis fund has closed.\nContact us for details.\n", "csv")
    assert "No header row found" in reason and not blocked


def test_validate_rejects_xlsx_without_holdings_header(tmp_path):
    reason, _ = rejected(tmp_path, xlsx_bytes([["Notice"], ["This fund has closed."],
                                               ["Contact us for details."]]), "xlsx")
    assert "No header row found" in reason


def test_validate_accepts_good_csv(tmp_path):
    assert list(accepted(tmp_path, GOOD_CSV, "csv")["Ticker"]) == ["XYZ", "ABC"]
