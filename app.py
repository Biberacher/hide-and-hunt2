from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os


# -------------------------
# App Konfiguration
# -------------------------

os.makedirs("database", exist_ok=True)
os.makedirs("uploads", exist_ok=True)

app = Flask(__name__)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///game_history.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = "uploads"


db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")


# -------------------------
# Datenbank Modelle
# -------------------------


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(
        db.String(100),
        nullable=False
    )

    status = db.Column(
        db.String(20),
        default="waiting"
    )

    start_time = db.Column(
        db.DateTime,
        nullable=True
    )

    hiding_time = db.Column(
        db.Integer,
        default=10
    )

    game_duration = db.Column(
        db.Integer,
        default=60
    )

    area_center_lat = db.Column(
        db.Float,
        nullable=True
    )

    area_center_lng = db.Column(
        db.Float,
        nullable=True
    )

    area_radius = db.Column(
        db.Float,
        nullable=True
    )


    teams = db.relationship(
        "Team",
        backref="game",
        cascade="all, delete"
    )



class Team(db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True
    )

    name = db.Column(
        db.String(100),
        nullable=False
    )

    type = db.Column(
        db.String(20),
        nullable=False
    )
    # hider oder seeker


    game_id = db.Column(
        db.Integer,
        db.ForeignKey("game.id")
    )


    players = db.relationship(
        "Player",
        backref="team",
        cascade="all, delete"
    )



class Player(db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True
    )

    name = db.Column(
        db.String(100),
        nullable=False
    )


    team_id = db.Column(
        db.Integer,
        db.ForeignKey("team.id")
    )


    locations = db.relationship(
        "Location",
        backref="player",
        cascade="all, delete"
    )



class Location(db.Model):

    id = db.Column(
        db.Integer,
        primary_key=True
    )


    player_id = db.Column(
        db.Integer,
        db.ForeignKey("player.id")
    )


    latitude = db.Column(
        db.Float,
        nullable=False
    )


    longitude = db.Column(
        db.Float,
        nullable=False
    )


    timestamp = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )



# -------------------------
# Webseiten
# -------------------------


@app.route("/")
def index():
    return render_template("index.html")



@app.route("/game")
def game():
    return render_template("game.html")

@app.route("/teams", methods=["GET", "POST"])
def teams():

    if request.method == "POST":

        team = Team(
            name=request.form["name"],
            type=request.form["type"],
            game_id=request.form["game_id"]
        )

        db.session.add(team)
        db.session.commit()

        return "Team wurde erstellt!"

    games = Game.query.all()

    return render_template(
        "teams.html",
        games=games
    )

@app.route("/players", methods=["GET", "POST"])
def players():

    if request.method == "POST":

        player = Player(
            name=request.form["name"],
            team_id=request.form["team_id"]
        )

        db.session.add(player)
        db.session.commit()

        return "Spieler wurde erstellt!"

    teams = Team.query.all()

    players = Player.query.all()

    return render_template(
        "players.html",
        teams=teams,
        players=players
    )
@app.route("/admin", methods=["GET", "POST"])
def admin():

    if request.method == "POST":

        new_game = Game(
            name=request.form["name"],
            status="waiting",
            hiding_time=int(request.form["hiding_time"]),
            game_duration=60,
            area_center_lat=float(request.form["lat"]),
            area_center_lng=float(request.form["lng"]),
            area_radius=float(request.form["radius"])
        )

        db.session.add(new_game)
        db.session.commit()

        return "Spiel wurde erfolgreich erstellt!"

    games = Game.query.all()

    return render_template(
    "admin.html",
    games=games
)



# -------------------------
# GPS Updates
# -------------------------


@socketio.on("location_update")
def handle_location_update(data):

    print("Neue Position:", data)

    socketio.emit(
        "location_response",
        data
    )



# -------------------------
# Start
# -------------------------


if __name__ == "__main__":

    with app.app_context():
        db.create_all()

    print("Hide & Hunt Server gestartet!")

    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=True
    )