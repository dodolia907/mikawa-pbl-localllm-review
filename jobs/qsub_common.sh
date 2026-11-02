# 投入スクリプトから source して使う。キューに応じた qsub の引数を QSUB_ARGS と QSUB_VARS に用意する。
#   QUEUE=iEduq(既定): IoT・AI基盤 yind00/01、NVIDIA A100 80GB x1
#   QUEUE=Eduq       : 計算サーバ ysnd00/01、AMD Instinct MI210 64GB x2
# 任意: NGPUS(使う GPU 数)、HOST(ノードを固定。例 yind01)
# ジョブスクリプト内の #PBS -q/-l/-v は、ここで指定した値で上書きされる。
QUEUE=${QUEUE:-iEduq}
case $QUEUE in
  iEduq) SIF=/common/Singularity_sif/prg_env_intel_2025.02.sif; MAX_GPUS=1; NODES="yind00 yind01" ;;
  Eduq)  SIF=/common/Singularity_sif/prg_env_amd_2025.02.sif;   MAX_GPUS=2; NODES="ysnd00 ysnd01" ;;
  *) echo "未対応のキューです: $QUEUE(iEduq か Eduq)" >&2; exit 1 ;;
esac
if [ -n "${HOST:-}" ] && [[ " $NODES " != *" $HOST "* ]]; then
  echo "$QUEUE のノードは $NODES です(HOST=$HOST)" >&2; exit 1
fi
NGPUS=${NGPUS:-1}
if (( NGPUS < 1 || NGPUS > MAX_GPUS )); then
  echo "$QUEUE で使える GPU は 1〜$MAX_GPUS 枚です(NGPUS=$NGPUS)" >&2; exit 1
fi
QSUB_ARGS=(-q "$QUEUE" -l "select=1:ncpus=8:mem=64gb:ngpus=$NGPUS${HOST:+:host=$HOST}")
# -v はジョブスクリプトの #PBS -v を丸ごと置き換えるので、コンテナの指定も必ず含める
QSUB_VARS="SINGULARITY_IMAGE=$SIF"
