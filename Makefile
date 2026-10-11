.PHONY: eval test serve traffic openapi

eval:
	uv run gw eval --gate

test:
	uv run pytest

serve:
	uv run gw serve --demo

traffic:
	uv run gw traffic

openapi:
	uv run gw openapi
