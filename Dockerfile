FROM python:3.11-slim-bookworm

WORKDIR /usr/src/app

COPY ./requirements.txt ./

RUN pip install --upgrade pip

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -qq -y --no-install-recommends \
    gcc \
    libffi-dev \
    rustc \
    zlib1g-dev \
    libjpeg-dev \
    libssl-dev \
    make \
    automake \
    g++ \
    subversion \
    python3-dev \
  && pip install -r requirements.txt \
  && pip cache purge \
  && apt-get remove -y gcc rustc \
  && apt-get autoremove -y \
  && apt-get autoclean -y \
  && apt-get clean -y \
  && rm -rf /var/lib/apt/lists/* \
  && rm -rf /usr/share/doc/*

ADD ./TwitchChannelPointsMiner ./TwitchChannelPointsMiner
ENTRYPOINT [ "python", "run.py" ]
