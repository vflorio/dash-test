#!/usr/bin/env bash
#
# generate.sh - Genera video di test e varianti di manifest DASH HbbTV per
#               diagnosticare i problemi di compatibilita' sulle TV Samsung/Tizen.
#
# Replica le caratteristiche del manifest DAI multi-periodo che non viene
# riprodotto solo sui Samsung (Period0 contenuto 20s interlacciato lang=pol +
# Period1 spot 10s progressivo lang=und, HE-AAC) e produce 7 varianti
# bisezionabili, ognuna delle quali isola UNA caratteristica sospetta.
#
# Uso:
#   ./generate.sh                 # genera tutto (encoding + manifest)
#   ./generate.sh --manifests     # rigenera solo gli MPD (media gia' presenti)
#   ./generate.sh --serve         # genera tutto e avvia un web server locale
#   ./generate.sh --help
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/work"
BUILD="$HERE/build"
OUT="$HERE/out"

CONTENT_DUR=20
AD_DUR=10
SEG=1.92          # durata segmento (s) -> 48 frame @25fps -> 24576 tick @12800
FPS=25
GOP=48            # 1.92s * 25fps

MODE="all"
for arg in "${@:-}"; do
  case "$arg" in
    --manifests|--manifests-only) MODE="manifests" ;;
    --serve) MODE="serve" ;;
    --help|-h)
      sed -n '2,20p' "$0"; exit 0 ;;
    "") ;;
    *) echo "Argomento sconosciuto: $arg"; exit 2 ;;
  esac
done

# --- Prerequisiti ----------------------------------------------------------
# Lo snap probing di X stampa "unable to open display" ed e' innocuo: lo zittiamo.
unset DISPLAY || true
command -v python3 >/dev/null || { echo "python3 non trovato"; exit 1; }

# Scegli una build ffmpeg con libfdk_aac: solo quella produce vero HE-AAC
# (mp4a.40.5). La build APT ha solo l'encoder nativo 'aac' (no SBR/HE-AAC),
# mentre lo snap di solito include libfdk_aac. Proviamo i candidati in ordine e
# teniamo la prima build disponibile come fallback se nessuna ha libfdk.
# Nota: NON usare "ffmpeg ... | grep" in pipeline perche' con pipefail il probe
# X di snap fa uscire ffmpeg !=0 e invaliderebbe il grep. Catturiamo prima.
FFMPEG=""
AAC_ENC="aac"
for cand in "${FFMPEG_BIN:-}" /usr/bin/ffmpeg /snap/bin/ffmpeg /usr/local/bin/ffmpeg ffmpeg; do
  [ -n "$cand" ] || continue
  command -v "$cand" >/dev/null 2>&1 || continue
  "$cand" -hide_banner -version >/dev/null 2>&1 || true   # warm-up (snap cold start)
  enc="$("$cand" -hide_banner -encoders 2>/dev/null || true)"
  [ -n "$FFMPEG" ] || FFMPEG="$cand"
  if printf '%s\n' "$enc" | grep -q libfdk_aac; then
    FFMPEG="$cand"; AAC_ENC="libfdk_aac"; break
  fi
done
[ -n "$FFMPEG" ] || { echo "ffmpeg non trovato"; exit 1; }
if [ "$AAC_ENC" != "libfdk_aac" ]; then
  echo "ATTENZIONE: nessuna build ffmpeg con libfdk_aac; uso 'aac' (HE-AAC non disponibile)."
fi

# ffprobe (APT) e' opzionale: se presente lo usiamo per verificare i codec reali.
FFPROBE="$(command -v ffprobe 2>/dev/null || true)"
echo "ffmpeg: $FFMPEG (AAC encoder: $AAC_ENC)${FFPROBE:+  |  ffprobe: $FFPROBE}"

FONT="$(fc-match -f '%{file}' sans 2>/dev/null || true)"
FONT_OPT=""
[ -n "$FONT" ] && FONT_OPT="fontfile=${FONT}:"

# --------------------------------------------------------------------------
# 1. Masters: due clip sorgente 1080p25 con etichette e timecode "bruciati"
#    (cosi' sulla TV vedi quale periodo/segmento sta riproducendo).
# --------------------------------------------------------------------------
make_master() {
  local out="$1" dur="$2" label="$3" tone="$4"
  [ -f "$out" ] && { echo "  master gia' presente: $(basename "$out")"; return; }
  echo "  master: $(basename "$out") (${dur}s)"
  "$FFMPEG" -y -hide_banner -loglevel error \
    -f lavfi -i "testsrc2=s=1920x1080:r=${FPS}:d=${dur}" \
    -f lavfi -i "sine=f=${tone}:r=48000:d=${dur}" \
    -map 0:v -map 1:a \
    -vf "drawtext=${FONT_OPT}text='${label}':x=50:y=60:fontsize=56:fontcolor=white:box=1:boxcolor=black@0.6,drawtext=${FONT_OPT}text='%{pts\:hms}  f=%{n}':x=50:y=150:fontsize=46:fontcolor=yellow:box=1:boxcolor=black@0.6" \
    -c:v libx264 -preset veryfast -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
    -pix_fmt yuv420p -crf 16 \
    -c:a aac -b:a 160k -ar 48000 -ac 2 \
    "$out"
}

# --------------------------------------------------------------------------
# 2. encode_group: da un master produce un "gruppo" DASH con 3 rendition video
#    (960x540 / 1280x720 / 1920x1080) + 1 traccia audio, nomenclatura segmenti
#    identica all'originale (init-stream$RepresentationID$.m4s / chunk-...).
#    Parametri: <master> <nome_gruppo> <he|lc> <0|1 interlaced>
# --------------------------------------------------------------------------
encode_group() {
  local src="$1" name="$2" aprof="$3" ilace="$4"
  local dir="$BUILD/$name"
  if [ -f "$dir/stream.mpd" ] && [ "$MODE" != "all" ]; then
    echo "  gruppo gia' presente: $name"; return
  fi
  rm -rf "$dir"; mkdir -p "$dir"
  echo "  encoding gruppo: $name (audio=$aprof interlaced=$ilace)"

  local il_filter="" il_flags="" x264_il=""
  if [ "$ilace" = "1" ]; then
    il_filter=",interlace=scan=tff"
    il_flags="-flags +ilme+ildct -field_order tt"
    x264_il=":tff=1"
  fi

  local aopt
  if [ "$aprof" = "he" ] && [ "$AAC_ENC" = "libfdk_aac" ]; then
    aopt="-c:a libfdk_aac -profile:a aac_he -b:a 96k"
  elif [ "$aprof" = "he" ]; then
    aopt="-c:a aac -b:a 96k"
  else
    aopt="-c:a ${AAC_ENC} -b:a 128k"
  fi

  # shellcheck disable=SC2086
  "$FFMPEG" -y -hide_banner -loglevel error -i "$src" \
    -filter_complex "[0:v]split=3[a][b][c];\
[a]scale=960:540${il_filter},format=yuv420p[v0];\
[b]scale=1280:720${il_filter},format=yuv420p[v1];\
[c]scale=1920:1080${il_filter},format=yuv420p[v2]" \
    -map "[v0]" -map "[v1]" -map "[v2]" -map 0:a \
    -c:v libx264 -preset veryfast $il_flags \
    -b:v:0 1800k -maxrate:v:0 1800k -bufsize:v:0 3600k \
    -b:v:1 3584k -maxrate:v:1 3584k -bufsize:v:1 7168k \
    -b:v:2 8000k -maxrate:v:2 8000k -bufsize:v:2 16000k \
    -level:v:0 3.1 -level:v:1 3.1 -level:v:2 4.0 \
    -x264opts "keyint=${GOP}:min-keyint=${GOP}:scenecut=0:open-gop=0${x264_il}" \
    -force_key_frames "expr:gte(t,n_forced*${SEG})" \
    $aopt -ar 48000 -ac 2 \
    -use_timeline 1 -use_template 1 -seg_duration "$SEG" \
    -adaptation_sets "id=0,streams=0,1,2 id=1,streams=3" \
    -init_seg_name "init-stream\$RepresentationID\$.m4s" \
    -media_seg_name "chunk-stream\$RepresentationID\$-\$Number%05d\$.m4s" \
    -f dash "$dir/stream.mpd"
}

# --------------------------------------------------------------------------
if [ "$MODE" != "manifests" ]; then
  mkdir -p "$WORK" "$BUILD"
  echo "== 1/3 Generazione master =="
  make_master "$WORK/content_master.mov" "$CONTENT_DUR" "PERIOD 0 - CONTENT (lang pol)" 1000
  make_master "$WORK/ad_master.mov"      "$AD_DUR"      "PERIOD 1 - AD (lang und)"      440

  # Master concatenato (contenuto + spot) per la variante single-period.
  if [ ! -f "$WORK/concat_master.mov" ]; then
    echo "  master: concat_master.mov"
    "$FFMPEG" -y -hide_banner -loglevel error \
      -i "$WORK/content_master.mov" -i "$WORK/ad_master.mov" \
      -filter_complex "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]" \
      -map "[v]" -map "[a]" \
      -c:v libx264 -preset veryfast -g "$GOP" -keyint_min "$GOP" -sc_threshold 0 \
      -pix_fmt yuv420p -crf 16 -c:a aac -b:a 160k -ar 48000 -ac 2 \
      "$WORK/concat_master.mov"
  fi

  echo "== 2/3 Encoding gruppi DASH =="
  encode_group "$WORK/content_master.mov" content_ildct_heaac he 1
  encode_group "$WORK/content_master.mov" content_prog_heaac  he 0
  encode_group "$WORK/content_master.mov" content_ildct_aaclc lc 1
  encode_group "$WORK/content_master.mov" content_prog_aaclc  lc 0
  encode_group "$WORK/ad_master.mov"      ad_prog_heaac       he 0
  encode_group "$WORK/ad_master.mov"      ad_ildct_heaac      he 1
  encode_group "$WORK/ad_master.mov"      ad_prog_aaclc       lc 0
  encode_group "$WORK/concat_master.mov"  concat_prog_heaac   he 0
fi

echo "== 3/3 Assemblaggio manifest =="
python3 "$HERE/build_manifests.py"

# --- Verifica codec reali (richiede ffprobe) -------------------------------
# Conferma che HE-AAC (mp4a.40.5) e AAC-LC (mp4a.40.2) siano davvero distinti e
# che l'interlacciamento sia presente dove atteso: cosi' sai che le varianti
# isolano effettivamente la caratteristica che vogliono testare.
# Nota: l'init segment da solo non porta profilo ne' field_order; occorre
# sondare init+primo chunk concatenati (un frammento decodificabile).
if [ -n "$FFPROBE" ] && [ "$MODE" != "manifests" ]; then
  echo "== Verifica codec (ffprobe) =="
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  for g in content_ildct_heaac content_prog_heaac content_ildct_aaclc \
           content_prog_aaclc ad_prog_heaac ad_ildct_heaac ad_prog_aaclc \
           concat_prog_heaac; do
    d="$BUILD/$g"
    [ -f "$d/chunk-stream3-00001.m4s" ] || { echo "  $g: (assente)"; continue; }
    cat "$d/init-stream2.m4s" "$d/chunk-stream2-00001.m4s" > "$tmp/v.mp4" 2>/dev/null
    cat "$d/init-stream3.m4s" "$d/chunk-stream3-00001.m4s" > "$tmp/a.mp4" 2>/dev/null
    acodec="$("$FFPROBE" -v error -select_streams a:0 \
      -show_entries stream=codec_name,profile -of csv=p=0 "$tmp/a.mp4" 2>/dev/null | tr -d '\n')"
    field="$("$FFPROBE" -v error -select_streams v:0 \
      -show_entries stream=field_order -of csv=p=0 "$tmp/v.mp4" 2>/dev/null | tr -d '\n')"
    mpdcodec="$(grep -o 'audio/mp4\" codecs=\"[^\"]*\"' "$d/stream.mpd" 2>/dev/null \
      | head -1 | sed 's/.*codecs=\"\([^\"]*\)\".*/\1/')"
    printf '  %-22s audio=%-14s profile=%-8s mpd=%-10s video.field_order=%s\n' \
      "$g" "${acodec%%,*}" "${acodec#*,}" "${mpdcodec:-?}" "${field:-?}"
  done
fi

echo
echo "Fatto. Varianti in: $OUT"
echo "Per testare su Samsung servi la cartella via HTTP e apri i singoli .mpd:"
echo "  cd \"$HERE\" && python3 -m http.server 8000"
echo "  http://<ip-del-pc>:8000/out/v00_baseline.mpd   (atteso: KO sui Samsung)"
echo "  http://<ip-del-pc>:8000/out/index.html         (anteprima desktop)"

if [ "$MODE" = "serve" ]; then
  echo
  echo "Avvio server su http://0.0.0.0:8000 (Ctrl+C per fermare)"
  cd "$HERE"
  exec python3 -m http.server 8000
fi
