.PHONY: install run ingest web test clean

# the source pipeline file (override: make run FILE=path/to/file.xlsx)
FILE ?= data/raw/pipeline_data.xlsx

install:
	pip install -e .

# run the full morning routine against FILE and write today's action queue
run:
	python -m sally run --file $(FILE) --sheet pipeline

# drop the day-2 batch in — adds new leads, skips already-handled ones
day2:
	python -m sally run --file $(FILE) --sheet new_drop_day2

# ingest an arbitrary new batch (separate file)
ingest:
	python -m sally run --file $(FILE)

# thin web view of the action queue
web:
	streamlit run src/sally/webview.py

test:
	pytest -q

clean:
	rm -f data/out/* *.db
