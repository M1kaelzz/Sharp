FROM ghcr.io/astral-sh/uv:python3.13-trixie

COPY ./runtime/pyproject.toml /sharp/pyproject.toml
COPY ./runtime/uv.lock /sharp/uv.lock
WORKDIR /sharp
RUN uv sync --frozen --no-install-project -i https://mirrors.aliyun.com/pypi/simple/

COPY ./runtime /sharp
RUN uv sync --frozen -i https://mirrors.aliyun.com/pypi/simple/

ENV TZ=Asia/Shanghai
