
import os
import json
import time
import uuid
import threading
import psycopg2
import redis
import pika

from flask import Flask, request, jsonify
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://evuser:evpass@localhost:5432/evdb"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RABBIT_URL = os.getenv(
    "RABBIT_URL", "amqp://guest:guest@localhost:5672/%2F"
)

cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    for attempt in range(30):
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS stations (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        location TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS chargers (
                        id SERIAL PRIMARY KEY,
                        station_id INT REFERENCES stations(id),
                        available BOOLEAN DEFAULT TRUE
                    );

                    CREATE TABLE IF NOT EXISTS reservations (
                        id SERIAL PRIMARY KEY,
                        user_id INT REFERENCES users(id),
                        charger_id INT REFERENCES chargers(id),
                        status TEXT DEFAULT 'Reserved'
                    );

                    CREATE TABLE IF NOT EXISTS sessions (
                        id SERIAL PRIMARY KEY,
                        reservation_id INT REFERENCES reservations(id),
                        status TEXT DEFAULT 'Charging'
                    );

                    INSERT INTO users (id, name)
                    VALUES (1, 'Demo User')
                    ON CONFLICT (id) DO NOTHING;

                    INSERT INTO stations (id, name, location)
                    VALUES (1, 'Station A', 'Chennai')
                    ON CONFLICT (id) DO NOTHING;

                    INSERT INTO chargers (id, station_id, available)
                    VALUES (101, 1, TRUE), (102, 1, TRUE)
                    ON CONFLICT (id) DO NOTHING;

                    SELECT setval(
                        pg_get_serial_sequence('users', 'id'),
                        GREATEST((SELECT MAX(id) FROM users), 1)
                    );
                    SELECT setval(
                        pg_get_serial_sequence('stations', 'id'),
                        GREATEST((SELECT MAX(id) FROM stations), 1)
                    );
                    SELECT setval(
                        pg_get_serial_sequence('chargers', 'id'),
                        GREATEST((SELECT MAX(id) FROM chargers), 1)
                    );
                    """)
            return
        except psycopg2.OperationalError:
            time.sleep(2)

    raise RuntimeError("Database unavailable")


def publish_event(event_type, data):
    """Send events to RabbitMQ."""
    connection = pika.BlockingConnection(
        pika.URLParameters(RABBIT_URL)
    )
    channel = connection.channel()
    channel.queue_declare(queue="ev_events", durable=True)

    channel.basic_publish(
        exchange="",
        routing_key="ev_events",
        body=json.dumps({
            "event": event_type,
            "data": data
        }),
        properties=pika.BasicProperties(delivery_mode=2)
    )
    connection.close()


def run_init():
    init_db()


@app.route("/")
def home():
    return jsonify({
        "service": "Smart EV Charging Platform",
        "instance": os.getenv("HOSTNAME", "local"),
        "services": [
            "Users", "Stations", "Chargers",
            "Reservations", "Charging Sessions"
        ]
    })


# Station service: Redis caching
@app.route("/api/stations")
def stations():
    location = request.args.get("location", "")
    key = "stations:" + location.lower()

    try:
        cached = cache.get(key)
        if cached:
            return jsonify(json.loads(cached))
    except redis.RedisError:
        pass

    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM stations
                WHERE location ILIKE %s
            """, (f"%{location}%",))
            rows = cur.fetchall()

    result = [dict(row) for row in rows]

    try:
        cache.setex(key, 30, json.dumps(result))
    except redis.RedisError:
        pass

    return jsonify(result)


# Charger service
@app.route("/api/chargers/<int:charger_id>/availability")
def availability(charger_id):
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, available FROM chargers WHERE id=%s",
                (charger_id,)
            )
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "Charger not found"}), 404

    return jsonify({
        "chargerId": row["id"],
        "available": row["available"]
    })


# Reservation service: atomic reservation prevents double booking
@app.route("/api/reservations", methods=["POST"])
def reserve():
    data = request.get_json(silent=True) or {}

    if not data.get("userId") or not data.get("chargerId"):
        return jsonify({"error": "userId and chargerId required"}), 400

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE chargers SET available=FALSE
                    WHERE id=%s AND available=TRUE
                    RETURNING id
                """, (data["chargerId"],))

                charger = cur.fetchone()

                if not charger:
                    return jsonify({
                        "error": "Charger unavailable or not found"
                    }), 409

                cur.execute("""
                    INSERT INTO reservations (user_id, charger_id)
                    VALUES (%s, %s) RETURNING id
                """, (data["userId"], data["chargerId"]))

                reservation_id = cur.fetchone()[0]

        try:
            cache.delete("stations:")
            publish_event("reservation_created", {
                "reservationId": reservation_id,
                "chargerId": data["chargerId"]
            })
        except Exception as error:
            app.logger.warning("Event delivery failed: %s", error)

        return jsonify({
            "reservationId": reservation_id,
            "status": "Reserved"
        }), 201

    except psycopg2.errors.ForeignKeyViolation:
        return jsonify({"error": "Invalid user or charger ID"}), 400


# Charging session service
@app.route("/api/sessions/start", methods=["POST"])
def start_session():
    data = request.get_json(silent=True) or {}
    reservation_id = data.get("reservationId")

    if not reservation_id:
        return jsonify({"error": "reservationId required"}), 400

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE reservations SET status='Charging'
                WHERE id=%s AND status='Reserved'
                RETURNING id
            """, (reservation_id,))

            if not cur.fetchone():
                return jsonify({
                    "error": "Invalid or already started reservation"
                }), 409

            cur.execute("""
                INSERT INTO sessions (reservation_id, status)
                VALUES (%s, 'Charging') RETURNING id
            """, (reservation_id,))

            session_id = cur.fetchone()[0]

    try:
        publish_event("charging_started", {"sessionId": session_id})
    except Exception as error:
        app.logger.warning("Event delivery failed: %s", error)

    return jsonify({
        "sessionId": session_id,
        "status": "Charging"
    }), 201


@app.route("/api/sessions/<int:session_id>/status")
def session_status(session_id):
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM sessions WHERE id=%s", (session_id,)
            )
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "Session not found"}), 404

    return jsonify(dict(row))


@app.route("/api/sessions/<int:session_id>/stop", methods=["POST"])
def stop_session(session_id):
    with db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.id, s.status, s.reservation_id, r.charger_id
                FROM sessions s
                JOIN reservations r ON r.id=s.reservation_id
                WHERE s.id=%s FOR UPDATE OF s, r
            """, (session_id,))
            row = cur.fetchone()

            if not row:
                return jsonify({"error": "Session not found"}), 404

            if row["status"] != "Charging":
                return jsonify({"error": "Session already ended"}), 409

            cur.execute(
                "UPDATE sessions SET status='Completed' WHERE id=%s",
                (session_id,)
            )
            cur.execute(
                "UPDATE reservations SET status='Completed' WHERE id=%s",
                (row["reservation_id"],)
            )
            cur.execute(
                "UPDATE chargers SET available=TRUE WHERE id=%s",
                (row["charger_id"],)
            )

    try:
        publish_event("charging_stopped", {"sessionId": session_id})
    except Exception as error:
        app.logger.warning("Event delivery failed: %s", error)

    return jsonify({"sessionId": session_id, "status": "Completed"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
