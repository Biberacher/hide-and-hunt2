from flask import Flask, render_template, request, jsonify, redirect, abort
from flask_socketio import SocketIO, join_room
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
import os
import math
import uuid


# -------------------------
# App Konfiguration
# -------------------------

# Name der Webseite - hier an einer einzigen Stelle aendern, wirkt sich
# automatisch auf Titel und Ueberschriften in allen Seiten aus.
APP_NAME = "Hunt 'n Hide"

# Spielregeln als Konstanten, damit sie an einer Stelle gepflegt werden.
MAX_TEAM_SIZE = 3
MAX_SEEKER_TEAMS = 3
MIN_HIDING_TIME_MINUTES = 7
MAX_HIDING_TIME_MINUTES = 10
HIDER_UPDATE_INTERVAL_SECONDS = 5 * 60
MIN_AREA_RADIUS_METERS = 500      # 1 km Durchmesser
MAX_AREA_RADIUS_METERS = 4000     # 8 km Durchmesser

ALLOWED_PHOTO_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "heic"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_DIR = os.path.join(BASE_DIR, "database")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

os.makedirs(DATABASE_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(DATABASE_DIR, "game_history.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB je Upload


db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")


@app.context_processor
def inject_globals():
    return dict(app_name=APP_NAME)


# -------------------------
# Datenbank Modelle
# -------------------------


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default="waiting")
    # waiting -> running -> ended

    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)

    hiding_time = db.Column(db.Integer, default=10)
    game_duration = db.Column(db.Integer, default=60)

    area_center_lat = db.Column(db.Float, nullable=True)
    area_center_lng = db.Column(db.Float, nullable=True)
    area_radius = db.Column(db.Float, nullable=True)

    # "hiders_survived" (Zeit abgelaufen) oder "seekers_caught" (Verstecker gefangen)
    result = db.Column(db.String(30), nullable=True)
    capture_photo = db.Column(db.String(255), nullable=True)

    teams = db.relationship("Team", backref="game", cascade="all, delete")

    def hunt_end_timestamp(self):
        """Unix-Zeitstempel, zu dem das Spiel automatisch endet (Ende der Suchphase)."""
        if not self.start_time:
            return None
        return self.start_time.replace(tzinfo=timezone.utc).timestamp() + (
            (self.hiding_time + self.game_duration) * 60
        )

    def hiding_end_timestamp(self):
        """Unix-Zeitstempel, zu dem die Versteckphase endet und die Sucher starten duerfen."""
        if not self.start_time:
            return None
        return self.start_time.replace(tzinfo=timezone.utc).timestamp() + (self.hiding_time * 60)


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    # hider oder seeker

    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False)

    players = db.relationship("Player", backref="team", cascade="all, delete")


class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)

    locations = db.relationship(
        "Location", backref="player", cascade="all, delete", order_by="Location.timestamp"
    )

    def latest_location(self):
        if not self.locations:
            return None
        return self.locations[-1]


class Location(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    player_id = db.Column(db.Integer, db.ForeignKey("player.id"), nullable=False)

    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)

    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


# -------------------------
# Hilfsfunktionen
# -------------------------


def parse_float(value, field_name):
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, f"'{field_name}' muss eine Zahl sein."


def parse_int(value, field_name):
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, f"'{field_name}' muss eine ganze Zahl sein."


def haversine_meters(lat1, lon1, lat2, lon2):
    """Entfernung zwischen zwei GPS-Punkten in Metern."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


TEAM_COLOR_HIDER = "#e74c3c"          # rot
SEEKER_COLOR_PALETTE = ["#3498db", "#2ecc71", "#9b59b6", "#f39c12"]


def get_team_color(team):
    """Feste Farbe fuers Verstecker-Team, unterschiedliche Farben je Sucher-Team."""
    if team.type == "hider":
        return TEAM_COLOR_HIDER

    seeker_teams = (
        Team.query.filter_by(game_id=team.game_id, type="seeker").order_by(Team.id).all()
    )
    try:
        position = [t.id for t in seeker_teams].index(team.id)
    except ValueError:
        position = 0
    return SEEKER_COLOR_PALETTE[position % len(SEEKER_COLOR_PALETTE)]


def serialize_location(player, location):
    team = player.team
    inside_area = None
    if (
        team.game.area_center_lat is not None
        and team.game.area_center_lng is not None
        and team.game.area_radius
    ):
        distance = haversine_meters(
            location.latitude,
            location.longitude,
            team.game.area_center_lat,
            team.game.area_center_lng,
        )
        inside_area = distance <= team.game.area_radius

    return {
        "player_id": player.id,
        "player_name": player.name,
        "team_id": team.id,
        "team_name": team.name,
        "team_type": team.type,
        "color": get_team_color(team),
        "latitude": location.latitude,
        "longitude": location.longitude,
        "timestamp": location.timestamp.replace(tzinfo=timezone.utc).isoformat(),
        "inside_area": inside_area,
    }


def check_and_end_game(game):
    """Beendet das Spiel automatisch, wenn die Gesamtzeit abgelaufen ist.
    Gibt True zurueck, wenn das Spiel dadurch gerade jetzt beendet wurde."""

    if game.status != "running" or not game.start_time:
        return False

    hunt_end = game.hunt_end_timestamp()
    if hunt_end is None or datetime.now(timezone.utc).timestamp() < hunt_end:
        return False

    game.status = "ended"
    game.result = "hiders_survived"
    game.end_time = datetime.now(timezone.utc)
    db.session.commit()

    socketio.emit(
        "game_ended",
        {"reason": "time", "result": game.result, "message": "Zeit abgelaufen - Verstecker gewinnen!"},
        room=f"game_{game.id}",
    )
    return True


def background_game_watcher():
    """Laeuft dauerhaft im Hintergrund und beendet Spiele automatisch,
    auch wenn gerade niemand aktiv seinen Standort sendet."""
    while True:
        socketio.sleep(15)
        with app.app_context():
            running_games = Game.query.filter_by(status="running").all()
            for g in running_games:
                check_and_end_game(g)


# -------------------------
# Webseiten
# -------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/game/<int:player_id>")
def game(player_id):

    player = Player.query.get(player_id)

    if not player:
        abort(404, description="Spieler wurde nicht gefunden.")

    team = player.team
    game = team.game if team else None

    if not game:
        abort(404, description="Zu diesem Spieler existiert kein Spiel.")

    check_and_end_game(game)

    now_ts = datetime.now(timezone.utc).timestamp()
    hiding_remaining = 0
    hunt_remaining = 0

    if game.status == "running" and game.start_time:
        hiding_remaining = max(0, int(game.hiding_end_timestamp() - now_ts))
        hunt_remaining = max(0, int(game.hunt_end_timestamp() - now_ts))

    return render_template(
        "game.html",
        player_id=player_id,
        player_name=player.name,
        team_type=team.type,
        team_color=get_team_color(team),
        game_id=game.id,
        game_status=game.status,
        game_result=game.result,
        hiding_remaining=hiding_remaining,
        hunt_remaining=hunt_remaining,
        area_center_lat=game.area_center_lat,
        area_center_lng=game.area_center_lng,
        area_radius=game.area_radius,
        hider_update_interval=HIDER_UPDATE_INTERVAL_SECONDS,
    )


@app.route("/join", methods=["GET", "POST"])
def join():

    teams = Team.query.all()

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        team_id_raw = request.form.get("team_id")

        if not name:
            return render_template("join.html", teams=teams, error="Bitte einen Namen eingeben."), 400

        team_id, err = parse_int(team_id_raw, "Team")
        team = Team.query.get(team_id) if not err else None

        if err or not team:
            return render_template("join.html", teams=teams, error="Bitte ein gültiges Team auswählen."), 400

        if len(team.players) >= MAX_TEAM_SIZE:
            return render_template(
                "join.html", teams=teams,
                error=f"Team '{team.name}' ist bereits voll (max. {MAX_TEAM_SIZE} Spieler)."
            ), 400

        player = Player(name=name, team_id=team_id)

        db.session.add(player)
        db.session.commit()

        return redirect(f"/game/{player.id}")

    return render_template("join.html", teams=teams)


@app.route("/teams", methods=["GET", "POST"])
def teams():

    games = Game.query.all()

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        team_type = request.form.get("type")
        game_id, err = parse_int(request.form.get("game_id"), "Spiel")
        game_obj = Game.query.get(game_id) if not err else None

        if not name:
            return render_template("teams.html", games=games, error="Bitte einen Teamnamen eingeben."), 400

        if team_type not in ("hider", "seeker"):
            return render_template("teams.html", games=games, error="Bitte einen gültigen Teamtyp wählen."), 400

        if err or not game_obj:
            return render_template("teams.html", games=games, error="Bitte ein gültiges Spiel auswählen."), 400

        if team_type == "hider":
            existing_hider = Team.query.filter_by(game_id=game_id, type="hider").count()
            if existing_hider >= 1:
                return render_template(
                    "teams.html", games=games,
                    error="Für dieses Spiel gibt es bereits ein Verstecker-Team."
                ), 400
        else:
            existing_seekers = Team.query.filter_by(game_id=game_id, type="seeker").count()
            if existing_seekers >= MAX_SEEKER_TEAMS:
                return render_template(
                    "teams.html", games=games,
                    error=f"Für dieses Spiel gibt es bereits die maximale Anzahl ({MAX_SEEKER_TEAMS}) an Sucher-Teams."
                ), 400

        team = Team(name=name, type=team_type, game_id=game_id)

        db.session.add(team)
        db.session.commit()

        return redirect("/teams")

    return render_template("teams.html", games=games, max_team_size=MAX_TEAM_SIZE)


@app.route("/players", methods=["GET", "POST"])
def players():

    teams = Team.query.all()

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        team_id, err = parse_int(request.form.get("team_id"), "Team")
        team = Team.query.get(team_id) if not err else None

        if not name:
            players_list = Player.query.all()
            return render_template(
                "players.html", teams=teams, players=players_list, error="Bitte einen Namen eingeben.",
                max_team_size=MAX_TEAM_SIZE,
            ), 400

        if err or not team:
            players_list = Player.query.all()
            return render_template(
                "players.html", teams=teams, players=players_list, error="Bitte ein gültiges Team wählen.",
                max_team_size=MAX_TEAM_SIZE,
            ), 400

        if len(team.players) >= MAX_TEAM_SIZE:
            players_list = Player.query.all()
            return render_template(
                "players.html", teams=teams, players=players_list,
                error=f"Team '{team.name}' ist bereits voll (max. {MAX_TEAM_SIZE} Spieler).",
                max_team_size=MAX_TEAM_SIZE,
            ), 400

        player = Player(name=name, team_id=team_id)

        db.session.add(player)
        db.session.commit()

        return redirect("/players")

    players_list = Player.query.all()

    return render_template("players.html", teams=teams, players=players_list, max_team_size=MAX_TEAM_SIZE)


@app.route("/admin", methods=["GET", "POST"])
def admin():

    games = Game.query.all()

    if request.method == "POST":

        name = request.form.get("name", "").strip()

        hiding_time, err1 = parse_int(request.form.get("hiding_time"), "Versteckzeit")
        lat, err2 = parse_float(request.form.get("lat"), "Breitengrad")
        lng, err3 = parse_float(request.form.get("lng"), "Längengrad")
        radius, err4 = parse_float(request.form.get("radius"), "Radius")

        error = next((e for e in (err1, err2, err3, err4) if e), None)

        if not name:
            error = "Bitte einen Spielnamen eingeben."

        if not error and hiding_time is not None and not (
            MIN_HIDING_TIME_MINUTES <= hiding_time <= MAX_HIDING_TIME_MINUTES
        ):
            error = f"Versteckzeit muss zwischen {MIN_HIDING_TIME_MINUTES} und {MAX_HIDING_TIME_MINUTES} Minuten liegen."

        if not error and radius is not None and not (
            MIN_AREA_RADIUS_METERS <= radius <= MAX_AREA_RADIUS_METERS
        ):
            error = (
                f"Radius muss zwischen {MIN_AREA_RADIUS_METERS} und {MAX_AREA_RADIUS_METERS} "
                f"Metern liegen (Spielgebiet 1-8 km Durchmesser)."
            )

        if error:
            return render_template("admin.html", games=games, error=error), 400

        new_game = Game(
            name=name,
            status="waiting",
            hiding_time=hiding_time,
            game_duration=60,
            area_center_lat=lat,
            area_center_lng=lng,
            area_radius=radius,
        )

        db.session.add(new_game)
        db.session.commit()

        return redirect("/admin")

    return render_template("admin.html", games=games)


@app.route("/start_game/<int:game_id>")
def start_game(game_id):

    game = Game.query.get(game_id)

    if not game:
        abort(404, description="Spiel wurde nicht gefunden.")

    game.status = "running"
    game.start_time = datetime.now(timezone.utc)
    game.result = None
    game.end_time = None

    db.session.commit()

    socketio.emit(
        "game_started",
        {"hiding_time": game.hiding_time, "game_duration": game.game_duration},
        room=f"game_{game.id}",
    )

    return redirect("/admin")


@app.route("/lobby/<int:game_id>")
def lobby(game_id):

    game = Game.query.get(game_id)

    if not game:
        abort(404, description="Spiel wurde nicht gefunden.")

    check_and_end_game(game)

    return render_template("lobby.html", game=game)


@app.route("/live_map/<int:game_id>")
def live_map(game_id):

    game = Game.query.get(game_id)

    if not game:
        abort(404, description="Spiel wurde nicht gefunden.")

    check_and_end_game(game)

    return render_template("live_map.html", game=game)


@app.route("/report_capture/<int:game_id>", methods=["POST"])
def report_capture(game_id):

    game = Game.query.get(game_id)

    if not game:
        abort(404, description="Spiel wurde nicht gefunden.")

    if game.status != "running":
        return jsonify({"ok": False, "error": "Das Spiel läuft gerade nicht."}), 400

    photo_filename = None
    photo = request.files.get("photo")

    if photo and photo.filename:
        ext = photo.filename.rsplit(".", 1)[-1].lower() if "." in photo.filename else ""
        if ext not in ALLOWED_PHOTO_EXTENSIONS:
            return jsonify({"ok": False, "error": "Ungültiges Bildformat."}), 400

        photo_filename = f"capture_{game_id}_{uuid.uuid4().hex}.{ext}"
        photo.save(os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(photo_filename)))

    game.status = "ended"
    game.result = "seekers_caught"
    game.end_time = datetime.now(timezone.utc)
    game.capture_photo = photo_filename

    db.session.commit()

    socketio.emit(
        "game_ended",
        {
            "reason": "caught",
            "result": game.result,
            "message": "Verstecker gefangen - Sucher gewinnen!",
        },
        room=f"game_{game.id}",
    )

    return jsonify({"ok": True})


# -------------------------
# JSON APIs
# -------------------------


@app.route("/api/game/<int:game_id>/state")
def api_game_state(game_id):

    game = Game.query.get(game_id)
    if not game:
        return jsonify({"error": "not_found"}), 404

    check_and_end_game(game)

    now_ts = datetime.now(timezone.utc).timestamp()
    hiding_remaining = 0
    hunt_remaining = 0
    if game.status == "running" and game.start_time:
        hiding_remaining = max(0, int(game.hiding_end_timestamp() - now_ts))
        hunt_remaining = max(0, int(game.hunt_end_timestamp() - now_ts))

    return jsonify(
        {
            "status": game.status,
            "result": game.result,
            "hiding_remaining": hiding_remaining,
            "hunt_remaining": hunt_remaining,
            "area_center_lat": game.area_center_lat,
            "area_center_lng": game.area_center_lng,
            "area_radius": game.area_radius,
        }
    )


@app.route("/api/game/<int:game_id>/locations")
def api_game_locations(game_id):

    game = Game.query.get(game_id)
    if not game:
        return jsonify({"error": "not_found"}), 404

    results = []
    for team in game.teams:
        for player in team.players:
            loc = player.latest_location()
            if loc:
                results.append(serialize_location(player, loc))

    return jsonify({"locations": results})


# -------------------------
# Fehlerseiten
# -------------------------


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", message=e.description or "Seite nicht gefunden."), 404


# -------------------------
# Echtzeit-Events
# -------------------------


@socketio.on("join_game")
def handle_join_game(data):
    if not isinstance(data, dict):
        return
    game_id, err = parse_int(data.get("game_id"), "game_id")
    if err:
        return
    join_room(f"game_{game_id}")


@socketio.on("location_update")
def handle_location_update(data):

    if not isinstance(data, dict):
        return

    player_id, err = parse_int(data.get("player_id"), "player_id")
    player = Player.query.get(player_id) if not err else None
    if err or not player:
        print("Ungültiges location_update ignoriert:", data)
        return

    try:
        latitude = float(data["latitude"])
        longitude = float(data["longitude"])
    except (KeyError, TypeError, ValueError):
        print("Ungültige Koordinaten ignoriert:", data)
        return

    team = player.team
    game = team.game

    if game.status != "running":
        return

    if check_and_end_game(game):
        return

    # Regelwerk: Verstecker senden ihren Standort nur alle 5 Minuten,
    # Sucher permanent. Server-seitig gegen Spam absichern.
    if team.type == "hider":
        last = player.latest_location()
        if last:
            last_ts = last.timestamp.replace(tzinfo=timezone.utc).timestamp()
            elapsed = datetime.now(timezone.utc).timestamp() - last_ts
            if elapsed < HIDER_UPDATE_INTERVAL_SECONDS - 5:
                return

    location = Location(player_id=player_id, latitude=latitude, longitude=longitude)

    db.session.add(location)
    db.session.commit()

    payload = serialize_location(player, location)

    socketio.emit("location_response", payload, room=f"game_{game.id}")

    # Warnung nur an den betroffenen Verstecker selbst, falls er das
    # Spielgebiet verlassen hat - koordinatenbasiert, echte Bewegungsfreiheit
    # kann softwareseitig natuerlich nicht verhindert werden.
    if team.type == "hider" and payload["inside_area"] is False:
        socketio.emit(
            "area_warning",
            {"message": "⚠️ Du bist außerhalb des Spielgebiets!"},
            room=f"game_{game.id}",
        )


# -------------------------
# Start
# -------------------------


if __name__ == "__main__":

    with app.app_context():
        db.create_all()

    print(f"{APP_NAME} Server gestartet!")

    socketio.start_background_task(background_game_watcher)

    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=True,
    )
