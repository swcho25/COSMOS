#!/usr/bin/env bash
set -euo pipefail
umask 027

project=${SENTIMENT_LOADER_PROJECT_DIR:-/opt/cosmos/news-enrich/current}
python=${SENTIMENT_LOADER_PYTHON:-/opt/cosmos/news-enrich/venv/bin/python}
root=${SENTIMENT_LOADER_ROOT:?set SENTIMENT_LOADER_ROOT to the model_version partition}
state=${SENTIMENT_LOADER_STATE:-/var/lib/cosmos-sentiment-loader/state.json}
hadoop_home=${HADOOP_HOME:-/opt/hadoop}

[[ -x "$python" ]] || { echo "SENTIMENT_LOADER_PYTHON is not executable" >&2; exit 2; }
: "${COSMOS_DSN:?set COSMOS_DSN to the Postgres connection string}"

export PYTHONPATH="$project/Crawling/documents${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1
export HADOOP_CONF_DIR=${HADOOP_CONF_DIR:-$hadoop_home/etc/hadoop}

cd "$project/Crawling/documents"
exec "$python" -u -m services.sentiment_loader.runner \
  --root "$root" \
  --state-file "$state" \
  --hdfs-bin "${HDFS_BIN:-$hadoop_home/bin/hdfs}" \
  --commit
