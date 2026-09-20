GUIDE ?= /home/vishnu/projects/resume guide

.PHONY: refresh-quals check-quals test

# The 128-title qualification sheet is vendored into the repo so the GitHub Actions
# runner has it without an S3 fetch. It is regenerated upstream by the guide's
# refresh_qualifications.py; this target copies the result in.
refresh-quals:
	python "$(GUIDE)/refresh_qualifications.py"
	cp "$(GUIDE)/job_qualifications.md" data/job_qualifications.md
	@echo "vendored -> data/job_qualifications.md"

# Fails if the vendored copy has drifted from the guide's.
check-quals:
	@diff -q "$(GUIDE)/job_qualifications.md" data/job_qualifications.md \
	  && echo "data/job_qualifications.md is current" \
	  || (echo "STALE: run 'make refresh-quals'" && exit 1)

test:
	pytest -q
