import io
import re
from typing import Dict, Any, List
import pandas as pd

KNOWN_VAR_KEYWORDS = {
    "temperature": ["temp", "t_deg_c", "t", "temperature", "temp_c"],
    "salinity": ["sal", "salinity", "s", "psu", "sal_psu"],
    "depth": ["depth", "dep", "z", "press", "pressure", "d_m"],
    "oxygen": ["oxygen", "o2", "dissolved_o2", "d_o", "s_6_surface-do"],
    "chlorophyll": ["chlorophyll", "chla", "chl_a", "pigment"],
    "latitude": ["lat", "latitude", "lat_deg"],
    "longitude": ["lon", "long", "longitude", "lon_deg"]
}


def profile_raw_data(preview_text: str, file_url_or_path: str = "") -> Dict[str, Any]:
    """
    Profiles raw file preview text to separate header metadata from tabular data,
    detect delimiters, column names, and scientific variables.
    """
    if not preview_text or not preview_text.strip():
        return {"error": "Empty preview text"}

    lines = preview_text.splitlines()

    # 1. Identify Header Comment Lines vs Data Lines
    comment_lines = []
    data_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Comments usually start with #, %, *, or contain key: value pairs
        if stripped.startswith(("#", "%", "*", "//")) or re.match(r"^[A-Za-z_\s]+:\s*.+", stripped):
            comment_lines.append(stripped)
        else:
            data_lines.append(stripped)

    raw_header_block = "\n".join(comment_lines)

    # Comments anywhere in the file belong in raw_header_block -- a coordinate can be
    # written below the table. But the count is used downstream as a SKIP count, so it must
    # describe only the leading block; a single mid-file annotation was otherwise enough to
    # skip the real column-header row and promote the first data row to be the header.
    leading_comment_lines = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            leading_comment_lines += 1
            continue
        if stripped.startswith(("#", "%", "*", "//")) or re.match(r"^[A-Za-z_\s]+:\s*.+", stripped):
            leading_comment_lines += 1
            continue
        break

    if not data_lines:
        return {
            "source": file_url_or_path,
            "format_family": "unstructured_text_or_metadata",
            "raw_header_block": raw_header_block,
            "columns": [],
            "inferred_dtypes": {}
        }

    # 2. Sniff Delimiter
    sample_data_str = "\n".join(data_lines[:20])
    delimiter = _sniff_delimiter(data_lines[0] if data_lines else "")

    # 3. Parse tabular preview with Pandas
    try:
        df = pd.read_csv(
            io.StringIO(sample_data_str),
            sep=delimiter,
            engine="python",
            on_bad_lines="skip"
        )
        columns = [str(c).strip() for c in df.columns]
        dtypes = {col: str(dtype) for col, dtype in df.dtypes.items()}
    except Exception as e:
        return {
            "source": file_url_or_path,
            "format_family": "custom_ascii_or_fixed_width",
            "raw_header_block": raw_header_block,
            "error_parsing_table": str(e),
            "raw_first_data_line": data_lines[0] if data_lines else None
        }

    # 4. Map Column Names to Known Scientific Variables
    detected_vars = []
    for col in columns:
        col_lower = col.lower()
        for var_std, keywords in KNOWN_VAR_KEYWORDS.items():
            if any(kw == col_lower or kw in col_lower for kw in keywords):
                if var_std not in detected_vars:
                    detected_vars.append(var_std)

    return {
        "source": file_url_or_path,
        "format_family": "tabular_ascii",
        "detected_delimiter": repr(delimiter) if delimiter != " " else "whitespace",
        "header_comment_lines": leading_comment_lines,
        "comment_lines_total": len(comment_lines),
        "raw_header_block": raw_header_block,
        "columns": columns,
        "inferred_dtypes": dtypes,
        "detected_scientific_variables": detected_vars
    }


def _sniff_delimiter(line: str) -> str:
    if "," in line:
        return ","
    elif "\t" in line:
        return "\t"
    elif ";" in line:
        return ";"
    else:
        return r"\s+"  # Whitespace split


if __name__ == "__main__":
    import json
    sample_txt = """# CRUISE: STOCS85A
# DATE: 1985-06-12
LAT: 61.930 LON: -172.270
Depth, Temp_C, Sal_PSU
0.0, 9.13, 34.94
10.0, 8.96, 34.91
25.0, 6.75, 34.92
"""
    res = profile_raw_data(sample_txt, "test_file.txt")
    print(json.dumps(res, indent=2))
