#!/usr/bin/env bash
# modelvault_provision.sh — stand up the encrypted rclone remotes ModelVault needs.
#
# One script, two backends, one crypt key:
#   GCS (default):  a dedicated bucket (ARCHIVE) + service account + two crypt
#                   remotes layered over the google-cloud-storage backend.
#   --local DIR:    two crypt remotes layered over a local/removable directory —
#                   the spec's "offline target", and the substrate the tests use.
#
# The crypt password+salt are generated ONCE and stored (obscured) inside the
# rclone.conf, which is the local mode-600 key file. They never leave this host
# and never enter the bucket. Idempotent: re-running reuses an existing conf.
#
# No silent failures: every prerequisite is checked and, on failure, the exact
# one-line fix is printed before exiting non-zero.
set -euo pipefail

MODE="gcs"
LOCAL_DIR=""
PROJECT="${MODELVAULT_GCP_PROJECT:-}"
PROJECT_HINT="agi_env_general"
BUCKET="${MODELVAULT_BUCKET:-}"
LOCATION="${MODELVAULT_GCS_LOCATION:-us}"
CONF="${MODELVAULT_RCLONE_CONF:-$HOME/.config/modelvault/rclone.conf}"
SA_KEY="${MODELVAULT_SA_KEY_FILE:-$HOME/.config/modelvault/sa.json}"
ARCHIVE="${MODELVAULT_REMOTE_ARCHIVE:-vault-archive}"
STANDARD="${MODELVAULT_REMOTE_STANDARD:-vault-standard}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local) MODE="local"; LOCAL_DIR="${2:?--local needs a DIR}"; shift 2 ;;
    --project) PROJECT="${2:?}"; shift 2 ;;
    --bucket) BUCKET="${2:?}"; shift 2 ;;
    --conf) CONF="${2:?}"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

die() { echo "FAIL: $1" >&2; [[ -n "${2:-}" ]] && echo "fix:  $2" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

have rclone || die "rclone not installed" "brew install rclone"

mkdir -p "$(dirname "$CONF")"
export RCLONE_CONFIG="$CONF"

# ----------------------------------------------------------- crypt secret (once)
# The conf is the key store. If a crypt remote already exists, reuse its secret.
# NB: `rclone config show <name>` exits 0 even for an absent remote, so we test
# presence with listremotes (a real check, not a silent default).
if rclone listremotes 2>/dev/null | grep -qx "${ARCHIVE}:"; then
  echo "reusing existing crypt remotes in $CONF"
  OBS_PASS="$(rclone config show "$ARCHIVE" | awk -F' = ' '/^password = /{print $2; exit}')"
  OBS_SALT="$(rclone config show "$ARCHIVE" | awk -F' = ' '/^password2 = /{print $2; exit}')"
  [[ -n "$OBS_PASS" && -n "$OBS_SALT" ]] || die "existing $ARCHIVE remote has no crypt secret" \
    "rm $CONF  and re-run to regenerate the key (WARNING: orphans any data already encrypted with the old key)"
else
  OBS_PASS="$(rclone obscure "$(head -c 32 /dev/urandom | base64)")"
  OBS_SALT="$(rclone obscure "$(head -c 32 /dev/urandom | base64)")"
fi

write_crypt() { # name  underlying_remote
  rclone config create "$1" crypt \
    remote="$2" password="$OBS_PASS" password2="$OBS_SALT" \
    filename_encryption=standard directory_name_encryption=true \
    --non-interactive >/dev/null
}

if [[ "$MODE" == "local" ]]; then
  mkdir -p "$LOCAL_DIR/archive" "$LOCAL_DIR/standard"
  write_crypt "$ARCHIVE"  "$LOCAL_DIR/archive"
  write_crypt "$STANDARD" "$LOCAL_DIR/standard"
  chmod 600 "$CONF"
  echo "provisioned LOCAL crypt remotes -> $LOCAL_DIR"
  echo "add to .env:  MODELVAULT_RCLONE_CONF=$CONF"
  exit 0
fi

# ------------------------------------------------------------------------- GCS
have gcloud || die "gcloud not installed" "brew install --cask google-cloud-sdk"
gcloud auth print-access-token >/dev/null 2>&1 \
  || die "gcloud is not authenticated (token expired)" "gcloud auth login"

if [[ -z "$PROJECT" ]]; then
  PROJECT="$(gcloud projects list --format='value(projectId)' 2>/dev/null \
            | grep -i "$PROJECT_HINT" | head -1 || true)"
  [[ -z "$PROJECT" ]] && PROJECT="$(gcloud projects list --format='value(projectId)' 2>/dev/null \
            | grep -iE 'agi.?env' | head -1 || true)"
fi
[[ -z "$PROJECT" ]] && die "no GCP project matched '$PROJECT_HINT'" \
  "pass --project <id>  (gcloud projects list)"
BUCKET="${BUCKET:-modelvault-$PROJECT}"
SA_EMAIL="modelvault@${PROJECT}.iam.gserviceaccount.com"
echo "project=$PROJECT bucket=gs://$BUCKET location=$LOCATION"

gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://$BUCKET" \
    --project="$PROJECT" --location="$LOCATION" \
    --default-storage-class=ARCHIVE --uniform-bucket-level-access \
    --public-access-prevention
gcloud storage buckets update "gs://$BUCKET" --versioning >/dev/null

gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create modelvault --project="$PROJECT" \
    --display-name="ModelVault cold-backup"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA_EMAIL" --role=roles/storage.objectAdmin >/dev/null
if [[ ! -s "$SA_KEY" ]]; then
  mkdir -p "$(dirname "$SA_KEY")"
  gcloud iam service-accounts keys create "$SA_KEY" --iam-account="$SA_EMAIL"
  chmod 600 "$SA_KEY"
fi

# Two gcs base remotes (ARCHIVE / STANDARD), then crypt over each.
for tier in archive:ARCHIVE standard:STANDARD; do
  name="gcs-${tier%%:*}"; klass="${tier##*:}"
  rclone config create "$name" "google cloud storage" \
    service_account_file="$SA_KEY" project_number="$PROJECT" \
    bucket_policy_only=true location="$LOCATION" storage_class="$klass" \
    --non-interactive >/dev/null
done
write_crypt "$ARCHIVE"  "gcs-archive:$BUCKET"
write_crypt "$STANDARD" "gcs-standard:$BUCKET"
chmod 600 "$CONF"

cat <<EOF
provisioned GCS vault.
add to .env:
  MODELVAULT_GCP_PROJECT=$PROJECT
  MODELVAULT_BUCKET=$BUCKET
  MODELVAULT_SA_KEY_FILE=$SA_KEY
  MODELVAULT_RCLONE_CONF=$CONF
EOF
