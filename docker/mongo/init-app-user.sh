set -eu

case "$MONGO_APP_USERNAME$MONGO_APP_PASSWORD" in
  *[!A-Za-z0-9_-]*)
    echo "Mongo application credentials may contain only letters, digits, underscores, and hyphens." >&2
    exit 1
    ;;
esac

"${mongo[@]}" "$MONGO_INITDB_DATABASE" <<-EOJS
db.createUser({
  user: $(_js_escape "$MONGO_APP_USERNAME"),
  pwd: $(_js_escape "$MONGO_APP_PASSWORD"),
  roles: [{ role: "readWrite", db: $(_js_escape "$MONGO_INITDB_DATABASE") }],
})
EOJS
