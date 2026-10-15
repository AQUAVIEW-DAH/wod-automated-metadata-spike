# Accession Field Guide

Ground truth about how **submissions actually arrive**, as a companion to
[`wod_code_tables/standardization_rules.md`](wod_code_tables/standardization_rules.md),
which is ground truth about what WOD requires.

Everything here was hit in a real accession. None of it is hypothetical, and none of it is
something the pipeline can infer for you — each item is a decision you have to make while
looking at the file.

---

## 1. What not to add

Decide this before doing any work on a folder. Wasted staging is cheap; a wrong ingest is not.

| Data | Why not |
| --- | --- |
| **Currents / ADCP** | WOD does not hold currents. No Table 3a code. But keep the *navigation* — see §1.1. |
| **Imagery — jpg, png, gif, mp4** | Not measurements. Already in `config.NON_DATA_EXTENSIONS`, so `process_accession` skips them, but say in your report that the folder was imagery rather than letting it vanish into `skipped`. |
| **Plankton biomass / taxonomy** | Belongs in WOD, but through the Biological Header (Table 6a) + Taxa-Records (Table 7a), **not** the profile path. See [`plankton_biomass.md`](plankton_biomass.md). Report it as an unfinished item, and capture the gear/tow/mesh/preservative metadata while you have the accession open — only the submitter has it. |
| **Bathymetry / soundings** | Bottom depth is secondary header 10 — per-cast metadata, not a measured variable. A sounding track has no profile in it. Not output, but not useless either — see §1.1. |
| **Nitrite, ammonium, CDOM, turbidity, PAHs, microplastics, eDNA** | No Table 3a code. Nitrite is deliberately excluded (manual p.49). |
| **Fish and mammal catch, seabird counts** | Not ocean profile variables. |
| **Anything 100% unanalysed** | Count real values per column first. A file whose every cell reads `Not analyzed` has no data, whatever its columns promise. |

**Documentation files are not a failure.** Readmes, cruise reports and format specs are where
the coordinates, units and sentinel legends are defined. Read them before deciding a dataset
cannot be staged.

### 1.1 "Not a dataset" is not "not useful"

The folders in the left column produce no cast of their own. Several of them are still the
best evidence in the accession about the casts you *are* adding, and reaching for them costs
one join.

`classify_dataset` surfaces this as a `harvest` field on every exclusion, and
`classify_accession` collects it per folder — so an excluded folder arrives already labelled
with what to take from it. **Nothing harvests automatically:** matching two files on position
and time is a judgement, so it happens during staging and the source file is cited in
`notes`.

| Folder | What it cannot be | What it can supply |
| --- | --- | --- |
| Multibeam / single-beam bathymetry | a profile | **secondary header 10, bottom depth**, at the cast position; and the sea-floor relief that settles **header 34, bottom hit** |
| ADCP / SADCP | a profile | a timestamped navigation track — the independent clock that tests a time convention (§5) |
| Cruise report, dive log | a profile | instrument model, cruise identity, ship-log times |

**Bottom depth from a bathymetry grid.** Ship-track multibeam is collected continuously, so a
station worked for any length of time usually sits within metres of a sounding. Take the
**median of the soundings within a few hundred metres** of the cast, not the single nearest
node: one node is a single beam over rough ground. Record the radius, the count and the
observed range in `notes`, because the value was derived and is not in the profile file.

**Bottom hit.** A drop that ends short of the probe's terminal depth, at a depth inside the
local sea-floor relief, struck the sea floor — that is header 34, and it changes how the
deepest levels should be read. Establish the terminal depth empirically from the accession's
other casts (a set of profiles all stopping dead at the same round depth is the probe's limit,
not the ocean's) rather than from a probe-type table you have not verified.

**Do not invent the codes.** Headers 3 (platform), 4 (institution) and 29 (probe type) take
numeric values from WOD's *website* code tables, which are not in the manual (Appendix 2
lists only a subset). Write the name the accession states and say it is a name,
or leave the header out. Never guess a probe type from a max depth.

---

## 2. Coordinates

### The forms, and what they mean

| Written as | Means | Decimal |
| --- | --- | --- |
| `49-46.9858N` | 49° 46.9858′ N — **degrees, hyphen, decimal minutes, hemisphere** | 49.783097 |
| `165-51.5694E` | 165° 51.5694′ E | 165.859490 |
| `62 9.6 N` | same thing, space-separated | 62.16 |
| `62 09 36 N` | degrees, minutes, **seconds** | 62.16 |
| `62`,`9.6`,`N` | three separate columns | 62.16 |
| `33.5 S` | decimal degrees with a hemisphere letter | −33.5 |

The hyphen is a **separator, not a minus sign**. `49-46.9858N` is not "49 minus 46". The
giveaway when you meet it: the minutes field is always < 60, and a file using this form very
often carries a decimal column beside it that you can check against. In accession 0287423
the underway DO file had both, and `49 + 46.8687/60 = 49.781145` matched its own decimal
column to every digit.

`parse_coordinate` reads all of these. **A hemisphere letter always beats a leading minus**,
because station lists routinely write both and the letter is what the originator meant.

### Non-English headers

Japanese-authored files are the common case and are usually **cp932**, which raises
`UnicodeDecodeError` on utf-8 and reads as a corrupt file when it is nothing of the kind.
`read_table` falls back through `config.TEXT_ENCODINGS` and reports which worked.

| Header | Meaning |
| --- | --- |
| 経度 | **longitude** |
| 緯度 | **latitude** |
| 度表記 | "expressed in degrees" — a units note, not a variable |
| 水深 | water depth |

**Do not assume latitude comes first.** The bathymetry file in 0287423 is ordered
`Date, 経度, 緯度, 水深` — longitude *before* latitude. Getting this backwards puts casts on
land, and a plausible-looking map is not proof. Check against another file in the accession,
or against the sample-location file NCEI generates in `about/`.

---

## 3. Table shapes

### A second header row holding units

```
time (UTC),latitude,longitude,water temperature,conductivity,salinity,chlorophyll
,degree N,degree E,degree C,mS/cm,,ppb
2023/6/11 1:40,40.26220667,145.5238417,15.728,42.589,34.062,0.62
```

Unit detection reads the **full column name and nothing else**, so a units row left in place
is invisible and every value is taken as already-standard. Fold it in with `fold_unit_row`
before staging — that is what makes `mg/l`, `ml/l` and `deg F` detectable.

### Fixed-width logger output with an undocumented flag column

```
# XBT  KM23-05                  20230303 2102 21-31.16N 144-01.84E 1831
        0.0      -5.00                             0N
        1.0      24.12                             0A
```

Underway loggers write a one-line header carrying everything the cast has — cruise, date,
time, position, level count — then fixed-width levels with no column names and no units.
Three things to settle before staging:

- **The trailing header number is usually the level count, not a depth.** Assert it against
  the row count; a mismatch means a truncated file.
- **An out-of-family value under a flag is a sentinel.** `-5.00` in a 24–28 °C surface layer
  is not a measurement. Blank the value, keep the level and keep the flag.
- **Undocumented flags stay untranslated.** If neither the accession's readme nor its cruise
  report defines the originator's flag alphabet, do not map it onto Table 12 — the codes are
  not standardised between submitters. Carry the column through staging verbatim so it lands
  in `unmapped_columns` and gets reported, quote the per-cast distribution in `notes`, and let
  the pipeline compute WOD's own flags independently.

### Metadata blocks, separated by blank lines or not at all

Submissions put a title, a legend, or a whole metadata block above or below the data, and
sometimes with no separator. Three variants and their handling:

- **A title row above the header** — `read_table(header_row=1)`. The index counts non-blank
  rows, so blank-line separation does not shift it.
- **A blank row between every data row** — files written with `\r\r\n`. Undetected this
  shifts the header and doubles the row count; `read_table` drops them and reports
  `blank_rows_dropped`.
- **A legend at the foot** — `*DL means "less than detection limit"...`. These group as if
  they were stations because the text sits in the station column, so a blank-key filter will
  not catch them. **Drop a group when its position will not parse**, and log what you
  dropped.
- **Two datasets in one file**, separated by a run of `,,,,,,`. Split on the separator row
  and treat each as its own table. Nothing detects this for you — you have to look.

### Missing-value legends

`**NS` not sampled · `***NA` not analysed · `*DL` below detection limit · `**ND` not
detected · `****ND` no data. The codes are **not standardised across submitters** and the
legend is usually in the last rows of the sheet or in the readme. `clean_sentinels` handles
the known set and takes `extra=` for the rest.

`*DL` and `**ND` are **real information** — the value lies under a stated threshold. WOD
Table 12 has no observed-level flag for it, so they still become missing, but the loss must
be quantified in your report and the detection limit must **not** be substituted in.

---

## 4. One cast, many files

Two shapes, both of which produce silently wrong output if not combined first.

**One file per depth.** Each file holds a single level of one cast. Left alone this becomes a
pile of single-level profiles instead of one profile. Combine with `combine_levels`, then
sort by depth.

**One file per variable.** Temperature in one file, oxygen in another, chemistry in a third,
all for the same cast or track. Combine with `combine_on_key` — joining on depth for a
profile, or on a timestamp for an underway track.

```python
cols, rows, info = combine_on_key([(c1, r1), (c2, r2)], key="Depth (m)",
                                  key_aliases=["DEPTH", "depth_m"])
```

**Check `info["per_table"]`.** A `matched_existing_keys` near zero means the files do not
actually share a key and the join produced two disjoint blocks rather than merged levels —
which looks like success and is not. In 0287423 the three underway streams shared their UTC
minute on 99.8% and 73.5% of records respectively, which is what justified joining them into
one SUR cast.

Also watch for the reverse: **one file holding many casts**, which needs `group_rows`
instead. A `Station` column with hundreds of rows is the tell.

---

## 5. Dates and times

**Everything must reach UTC.** Sources arrive as UTC, as a local zone (ET, JST, ship's
clock), or as a local stamp beside a separate offset column. That last one is the trap: a
column *labelled* `Date time (UTC)` sitting next to a `Time zone` column is ambiguous until
you test it, because the label is often wrong.

**Verify rather than assume.** If the accession has an underway GPS track, check the station
times against it — the correct interpretation puts the ship at the cast position. In 0287423
this settled it in one pass: read as UTC the error was 0.0 km across all 80 casts; read as
local-plus-offset it was 66.6 km. An ADCP folder you are not ingesting still carries this
track; reduce it to one position per ensemble and keep the reduced copy under `data/staging/`
with its source URL, labelled as verification rather than output.

**A stationary ship makes the track test meaningless.** The test works because the ship moves:
at the wrong offset it should be somewhere else. Over a seamount worked for hours, or a station
held overnight, every offset "matches" to a few hundred metres. Before trusting a match, check
that the track actually moves across the offsets you are comparing, and check whether the cast
time falls inside a gap in the track rather than on it.

**A track test proves one clock, not the zone.** Matching cast positions against a track shows
the two files share a clock — it cannot show that clock is UTC, because both sides moved
together. When nothing in the accession names a zone, say so in `notes` *and* in the report,
state which convention you recorded, and give the evidence you do have. In 0283273 the XBT and
SADCP clocks agreed to 0.0–0.4 km at zero offset against 1.7–3.7 km at ±9 h and ±10 h, and no
file in the accession names a zone; UTC was recorded as an assumption, flagged as one.

**A four-digit `HHMM` beside a date is a silent corruption risk.** `strptime` reads `2102`
under `%H%M%S` as 21:00:02 rather than failing, so the minute is wrong and nothing raises.
`staging.parse_timestamp` punctuates 4- and 6-digit time fields before parsing; if you write
your own reader, do the same.

### "Julian" in WOD does not mean the Julian calendar

This is worth being exact about, because the names collide:

| Term | What it actually is |
| --- | --- |
| **Julian year-day** (Table 3a variable **32**) | **Day-of-year within one year**, for SUR/APB/UOR only, where each observation carries its own time. Not a calendar. |
| **Secondary header 99** | A time stamp written `YYYYJJJ`, where `JJJ` is the year-day. |
| **Julian calendar** | Does not appear in the manual at all. Neither does "proleptic Gregorian". |

So there is **no Julian-calendar conversion step**. What WOD stores is a primary-header
year / month / day / time with a precision field, plus year-day as a measured variable in the
three datasets that need per-observation time.

Two live gotchas:

- **Year-day is specified inconsistently.** Manual p.37 defines year-day 0.00 as 00:00 on
  1 January of the year of the first measurement, then gives a worked example (00:00 on
  31 Dec 1965 → 365.00) implying one more. Pick one, record it in the staged header, flag it.
- **A day of 0 is legal.** The manual keeps submissions with a day of zero as-is, in case
  better information arrives. Do not "fix" one into day 1.

---

## 6. NetCDF inputs

**Not yet supported.** Discovery reads delimited text; there is no NetCDF reader, so an
accession delivered as NetCDF cannot currently be ingested.

Worth knowing for when it is built: originators frequently store **metadata as variables**
rather than as attributes — a `station`, `cruise`, `platform` or `date` array of length
`n_casts` sitting alongside the data. A reader that only walks `ncattrs()` will find an
apparently metadata-free file and drop all of it. Both places have to be checked, and a
scalar or all-identical variable is usually metadata rather than a measurement.

---

## 7. The intermediate format, and the single conversion routine

The point of this pipeline is that **one routine can turn its output into WOD binary**,
because every file it writes obeys the same contract regardless of what arrived. Anything
written against these guarantees does not need to know about staging, encodings or sentinels.

Every output file guarantees:

| | |
| --- | --- |
| **Layout** | CF-1.8 contiguous ragged array, `featureType = profile`. Dimensions `casts` and `z_obs`; `z_row_size` on `casts` with `sample_dimension = z_obs`. |
| **Alignment** | `z` **and every measured variable** live on the shared `z_obs` dimension, so a measurement is always index-aligned with its depth. Per-variable observation dimensions are a hard validation error. |
| **Per cast** | `lat`, `lon`, `time`, `cast_id` (`cf_role = profile_id`). |
| **Per level** | `z` (`positive = down`, metres), `z_qc`, one variable per WOD code, and a `<var>_qc` for each. |
| **Identification** | Every measured variable carries `wod_variable_code`, `units` as the UDUNITS string of the Table 3a **standard** unit (`degree_C`, `1`, `umol kg-1`) so CF readers parse it, `wod_units` with Table 3a's own wording (`Degrees Celsius`), and a CF `standard_name` where one exists — conversion has already happened. Missing values are the `_FillValue`. |
| **Flags** | Table 12 values, with `flag_values` and `flag_meanings`. Nothing is ever deleted. |
| **Metadata** | `secondary_header_<id>_<name>` attributes, keyed by Table 4a ID. |
| **Provenance** | `unit_conversions`, and `seawater_density_kg_m3` + `seawater_density_provenance` whenever an assumed density was used. |

Two things the converter must be told rather than infer, because the manual specifies
neither and they are **implementer's choices**:

- **The time epoch.** `time` is `days since 1770-01-01 00:00:00` with `calendar = "gregorian"`.
  WOD itself stores year/month/day, so the converter has to decode this back. Note that
  `"gregorian"` is a deprecated CF alias for `"standard"`; since all WOD data postdates 1582
  it is numerically identical to `proleptic_gregorian`, so the choice is harmless — but it
  should be stated, not discovered.
- **Depth is not a variable.** `z` is the vertical axis; WOD code 25 is *Pressure*, a separate
  measurement that may also be present.

Still missing before the conversion is genuinely one routine: interpolation to standard
levels (Reiniger-Ross, p.56), the density check that would populate depth flag 2, and
basin-aware range bounds. See *Known limitations* in the top-level README.
