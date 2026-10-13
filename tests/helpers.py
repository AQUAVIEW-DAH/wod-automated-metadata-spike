"""Shared fixtures for the writer and verify suites -- a minimal cast that passes."""
import numpy as np
import pandas as pd


def make_cast(lat=61.93, lon=-172.27, rows=3, dic=False):
    df = pd.DataFrame({
        "Depth": np.arange(rows, dtype=float) * 10,
        "Depth_qc_flag": np.zeros(rows, dtype=int),
        "Temp_C": np.linspace(20.0, 18.0, rows),
        "Temp_C_qc_flag": np.zeros(rows, dtype=int),
    })
    mapping = [
        {"raw_column": "Depth", "wod_code": 0, "wod_name": "Depth", "target_unit": "meters"},
        {"raw_column": "Temp_C", "wod_code": 1, "wod_name": "Temperature",
         "target_unit": "Degrees Celsius"},
    ]
    if dic:
        df["DIC"] = np.linspace(2100.0, 2110.0, rows)
        df["DIC_qc_flag"] = np.zeros(rows, dtype=int)
        mapping.append({"raw_column": "DIC", "wod_code": 21,
                        "wod_name": "Dissolved inorganic carbon",
                        "target_unit": "Millimole liter-1"})
    contract = {
        "source": "test", "wod_dataset_code": "CTD",
        "parsed_coordinates": {"latitude": lat, "longitude": lon,
                               "year": 1985, "month": 6, "day": 12},
        "extracted_header_metadata": {"3": {"name": "Platform Code", "value": "STOCS85A"}},
        "column_mapping": mapping,
    }
    return {"df": df, "contract": contract}


def cache_code_list(cache_dir, field, text, retrieved="2026-01-01T00:00:00+00:00"):
    """Put a published code list into the (test) cache, as if fetched on `retrieved`."""
    import json
    from pathlib import Path

    from wod_etl.reference import code_lists

    d = Path(cache_dir)
    (d / f"{field}.csv").write_text(text, encoding="utf-8")
    (d / f"{field}.json").write_text(json.dumps({"url": "test", "retrieved": retrieved}))
    code_lists.clear_cache_memo()
