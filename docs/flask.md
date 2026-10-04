# flask

## app basics
from flask import Flask                  # Import Flask
app = Flask(__name__)                     # Create the app instance
app.run(debug=True)                       # Run the dev server with auto-reload
app.run(host="0.0.0.0", port=5000)        # Run bound to a specific host/port
if __name__ == "__main__": app.run()      # Standard entry-point guard

## routing
@app.route("/")                           # Route for GET /
@app.route("/users", methods=["POST"])    # Restrict a route to specific HTTP methods
@app.route("/user/<int:id>")              # Route with a typed URL parameter
@app.route("/user/<string:name>")         # Route with a string URL parameter
url_for("index")                          # Build a URL for a view function by name

## request & response
request.method                            # The HTTP method of the current request
request.args.get("q")                     # Read a query string parameter
request.form.get("name")                  # Read a form field from POST data
request.json                              # Parse the request body as JSON
request.files.get("upload")               # Access an uploaded file
return "text", 201                        # Return a body with an explicit status code
return jsonify({"ok": True})              # Return a JSON response
return redirect(url_for("index"))         # Redirect to another view
abort(404)                                # Short-circuit the request with an HTTP error

## templates
render_template("index.html", name=name)  # Render a Jinja2 template with context
{{ variable }}                            # Output a variable in a template
{% for item in items %}                   # Loop inside a template
{% if condition %}                        # Conditional inside a template
{% extends "base.html" %}                 # Inherit from a base template
{% block content %}{% endblock %}         # Define a block a child template can override

## blueprints
bp = Blueprint("users", __name__)         # Create a blueprint
app.register_blueprint(bp, url_prefix="/users")  # Register a blueprint on the app

## config & extensions
app.config["SECRET_KEY"] = "..."          # Set a single config value
app.config.from_object(Config)            # Load config from a class/object
from flask_sqlalchemy import SQLAlchemy   # Common extension: ORM
db = SQLAlchemy(app)                      # Initialize an extension with the app
from flask_cors import CORS               # Common extension: CORS
CORS(app)                                 # Enable CORS on all routes

## sessions & flash
session["user_id"] = user.id              # Store a value in the session
session.pop("user_id", None)              # Remove a session value
flash("Saved!")                           # Queue a one-time message
get_flashed_messages()                    # Retrieve queued flash messages (in templates)

## errors
@app.errorhandler(404)                    # Register a custom handler for an HTTP error
@app.before_request                       # Run a function before every request
@app.after_request                        # Run a function after every request
