# StockScribe

StockScribe reads an article, extracts stock mentions, snapshots Yahoo Finance daily history, and summarizes each stock from the initial date to the ending date.

## Usage

Start the local UI:

```bash
python3 -B app.py
```

Then open `http://127.0.0.1:8000`.

Run with inline text:

```bash
python3 stock_scribe.py \
  --article "2024年1月到2024年3月，台積電 2330、鴻海 2317 和 $AAPL 都被提到。" \
  --output snapshot.json
```

Run with a text file and an explicit range:

```bash
python3 stock_scribe.py \
  --article-file article.txt \
  --start 2024-01-01 \
  --end 2024-03-31 \
  --market auto \
  --output snapshot.json
```

## Stock Detection

- Taiwan numeric codes such as `2330` are mapped to Yahoo symbols such as `2330.TW`.
- Explicit Yahoo symbols such as `2330.TW`, `AAPL`, and `$TSM` are kept as Yahoo symbols.
- Use `--market tpex` when four-digit Taiwan codes should map to `.TWO`.
- Common uppercase article words such as `API`, `CEO`, and `ETF` are ignored.

## Date Range

- If `--start` and `--end` are provided, that range is used.
- If the article contains two or more dates, the earliest and latest dates are used.
- If the article contains one date, StockScribe treats it as the start date and uses today as the end date.
- If no date is found, it snapshots the last 30 days.

## Summary Fields

The JSON snapshot includes one `histories` entry per Yahoo symbol with daily OHLCV records.

Each summary includes requested and actual trading dates, trading day count, start close, end close, price change, percent change, highest close, lowest close, average close, and total volume.
