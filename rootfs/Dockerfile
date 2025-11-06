ARG CODENAME
FROM registry.drycc.cc/drycc/base:${CODENAME}

ARG DRYCC_UID=1001 \
  DRYCC_GID=1001 \
  DRYCC_HOME_DIR=/usr/share/grafana \
  GRAFANA_VERSION="12.1.1" \
  JQ_VERSION="1.7.1" \
  CADDY_VERSION="2.10.2" \
  PYTHON_VERSION="3.13"

RUN groupadd drycc --gid ${DRYCC_GID} \
  && useradd drycc -u ${DRYCC_UID} -g ${DRYCC_GID} -s /bin/bash -m -d ${DRYCC_HOME_DIR}

COPY . /

RUN install-packages unzip fontconfig \
  && install-stack grafana ${GRAFANA_VERSION} \
  && install-stack jq ${JQ_VERSION} \
  && install-stack caddy $CADDY_VERSION \
  && install-stack python $PYTHON_VERSION && . init-stack \
  && python3 -m venv ${DRYCC_HOME_DIR}/venv \
  && source ${DRYCC_HOME_DIR}/venv/bin/activate \
  && pip3 install --disable-pip-version-check --no-cache-dir -r ${DRYCC_HOME_DIR}/oauth2/requirements.txt \
  && chown -R ${DRYCC_UID}:${DRYCC_GID} ${DRYCC_HOME_DIR} \
  # set env
  && echo "source ${DRYCC_HOME_DIR}/venv/bin/activate" >> /opt/drycc/python/profile.d/python.sh \
  # grafana plugins
  && mkdir ${DRYCC_HOME_DIR}/plugins \
  && grafana cli --pluginsDir=${DRYCC_HOME_DIR}/plugins --homepath=/opt/drycc/grafana plugins install grafana-llm-app \
  && grafana cli --pluginsDir=${DRYCC_HOME_DIR}/plugins --homepath=/opt/drycc/grafana plugins install quickwit-quickwit-datasource \
  # clean
  && rm -rf \
      /usr/share/doc \
      /usr/share/man \
      /usr/share/info \
      /usr/share/locale \
      /var/lib/apt/lists/* \
      /var/log/* \
      /var/cache/debconf/* \
      /etc/systemd \
      /lib/lsb \
      /lib/udev \
      /usr/lib/`echo $(uname -m)`-linux-gnu/gconv/IBM* \
      /usr/lib/`echo $(uname -m)`-linux-gnu/gconv/EBC* \
  && mkdir -p /usr/share/man/man{1..8} \
  && mkdir /var/lib/grafana -p \
  && chown -R ${DRYCC_UID}:${DRYCC_GID} /opt/drycc/grafana /usr/share/grafana /var/lib/grafana

USER ${DRYCC_UID}
WORKDIR ${DRYCC_HOME_DIR}
EXPOSE 3000
