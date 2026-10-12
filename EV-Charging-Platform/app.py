from flask import Flask, request, jsonify

app = Flask(__name__)

# Sample charging station data
stations = [
    {"id": 1, "name": "Station A", "location": "Chennai"},
    {"id": 2, "name": "Station B", "location": "Coimbatore"}
]

chargers = {
    101: {"available": True},
    102: {"available": True}
}

reservations = {}
sessions = {}
next_reservation_id = 1
next_session_id = 1


# 1. Search Charging Stations
@app.route("/api/stations", methods=["GET"])
def search_stations():
    location = request.args.get("location", "")
    result = [
        s for s in stations
        if location.lower() in s["location"].lower()
    ]
    return jsonify(result), 200


# 2. Check Charger Availability
@app.route("/api/chargers/<int:charger_id>/availability",
           methods=["GET"])
def availability(charger_id):
    charger = chargers.get(charger_id)

    if charger is None:
        return jsonify({"error": "Charger not found"}), 404

    return jsonify({
        "chargerId": charger_id,
        "available": charger["available"]
    }), 200


# 3. Reserve Charger
@app.route("/api/reservations", methods=["POST"])
def reserve_charger():
    global next_reservation_id

    data = request.get_json(silent=True) or {}
    charger_id = data.get("chargerId")
    user_id = data.get("userId")

    if charger_id is None or user_id is None:
        return jsonify({"error": "userId and chargerId required"}), 400

    charger_id = str(charger_id)

    if not charger_id.isdigit():
        return jsonify({"error": "Invalid chargerId"}), 400

    charger_id = int(charger_id)

    if charger_id not in chargers:
        return jsonify({"error": "Charger not found"}), 404

    if not chargers[charger_id]["available"]:
        return jsonify({"error": "Charger already reserved"}), 409

    reservation_id = next_reservation_id
    next_reservation_id += 1

    reservations[reservation_id] = {
        "userId": user_id,
        "chargerId": charger_id,
        "status": "Reserved"
    }

    chargers[charger_id]["available"] = False

    return jsonify({
        "reservationId": reservation_id,
        "chargerId": charger_id,
        "status": "Reserved",
        "message": "Charger reserved successfully"
    }), 201


# 4. Start Charging Session
@app.route("/api/sessions/start", methods=["POST"])
def start_charging():
    global next_session_id

    data = request.get_json(silent=True) or {}
    reservation_id = data.get("reservationId")

    if reservation_id is None:
        return jsonify({"error": "reservationId required"}), 400

    try:
        reservation_id = int(reservation_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid reservationId"}), 400

    reservation = reservations.get(reservation_id)

    if not reservation or reservation["status"] != "Reserved":
        return jsonify({"error": "Valid reservation not found"}), 404

    session_id = next_session_id
    next_session_id += 1

    sessions[session_id] = {
        "reservationId": reservation_id,
        "status": "Charging",
        "energyConsumed": 0
    }

    reservation["status"] = "Charging"

    return jsonify({
        "sessionId": session_id,
        "status": "Charging"
    }), 201


# 5. Get Charging Status
@app.route("/api/sessions/<int:session_id>/status",
           methods=["GET"])
def charging_status(session_id):
    session = sessions.get(session_id)

    if session is None:
        return jsonify({"error": "Session not found"}), 404

    return jsonify(session), 200


# 6. End Charging Session
@app.route("/api/sessions/<int:session_id>/stop",
           methods=["POST"])
def stop_charging(session_id):
    session = sessions.get(session_id)

    if session is None:
        return jsonify({"error": "Session not found"}), 404

    if session["status"] != "Charging":
        return jsonify({"error": "Session already ended"}), 409

    session["status"] = "Completed"

    reservation = reservations[session["reservationId"]]
    reservation["status"] = "Completed"

    charger_id = reservation["chargerId"]
    chargers[charger_id]["available"] = True

    return jsonify({
        "sessionId": session_id,
        "status": "Completed",
        "message": "Charging session ended"
    }), 200
@app.route("/")
def home():
    return """
    <h1>Smart EV Charging Platform</h1>
    <p>Welcome to the EV Charging API!</p>
    <p>Available APIs:</p>
    <ul>
        <li>/api/stations?location=Chennai</li>
        <li>/api/chargers/101/availability</li>
    </ul>
    """


if __name__ == "__main__":
    app.run(debug=True)
