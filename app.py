from flask import Flask

app = Flask(__name__)

@app.route("/")
def startseite():
    return """
    <h1>Hide & Hunt</h1>
    <p>Das Versteckspiel läuft!</p>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
