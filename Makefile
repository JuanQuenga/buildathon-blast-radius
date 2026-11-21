# Local helpers. KIT points at the unpacked official starter kit (not vendored).
KIT ?= ../../blast-radius/starter/sample-kit
SAMPLE ?= $(KIT)/sample
IMAGE ?= blast-radius-entry
OUTPUT ?= output.jsonl

# Same isolation flags as the official starter Makefile.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(abspath $(SAMPLE))/commits:/data/commits:ro \
	--volume $(abspath $(SAMPLE))/tests.json:/data/tests.json:ro

.PHONY: build sample local test

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 $(KIT)/score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# No Docker: run the entrypoint directly against the sample folder.
local:
	BLAST_DATA=$(abspath $(SAMPLE)) python3 -m selector.main > $(OUTPUT)
	python3 $(KIT)/score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

test:
	python3 -m unittest discover -s tests_selector -t . -v
