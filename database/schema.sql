BEGIN
  EXECUTE IMMEDIATE 'DROP TABLE parking_audit_log CASCADE CONSTRAINTS';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP TABLE reservations CASCADE CONSTRAINTS';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP TABLE parking_sessions CASCADE CONSTRAINTS';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP TABLE spots CASCADE CONSTRAINTS';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP TABLE users CASCADE CONSTRAINTS';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP SEQUENCE seq_user_id';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP SEQUENCE seq_spot_id';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP SEQUENCE seq_session_id';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP SEQUENCE seq_reservation_id';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

BEGIN
  EXECUTE IMMEDIATE 'DROP SEQUENCE seq_audit_id';
EXCEPTION WHEN OTHERS THEN NULL;
END;
/

CREATE SEQUENCE seq_user_id START WITH 1 INCREMENT BY 1 NOCACHE;
CREATE SEQUENCE seq_spot_id START WITH 1 INCREMENT BY 1 NOCACHE;
CREATE SEQUENCE seq_session_id START WITH 1 INCREMENT BY 1 NOCACHE;
CREATE SEQUENCE seq_reservation_id START WITH 1 INCREMENT BY 1 NOCACHE;
CREATE SEQUENCE seq_audit_id START WITH 1 INCREMENT BY 1 NOCACHE;

CREATE TABLE users (
    user_id NUMBER PRIMARY KEY,
    username VARCHAR2(100) NOT NULL UNIQUE,
    email VARCHAR2(150) NOT NULL UNIQUE,
    password_hash VARCHAR2(255),
    phone VARCHAR2(20),
    money_balance NUMBER(10, 2) DEFAULT 100.00 CHECK (money_balance >= 0),
    user_type VARCHAR2(20) DEFAULT 'REGULAR' CHECK (user_type IN ('REGULAR', 'PREMIUM', 'VIP')),
    role VARCHAR2(20) DEFAULT 'USER' CHECK (role IN ('USER', 'ADMIN')),
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
    updated_at TIMESTAMP DEFAULT SYSTIMESTAMP,
    is_active CHAR(1) DEFAULT 'Y' CHECK (is_active IN ('Y', 'N'))
);

CREATE TABLE spots (
    spot_id NUMBER PRIMARY KEY,
    spot_number VARCHAR2(20) NOT NULL UNIQUE,
    spot_type VARCHAR2(20) DEFAULT 'STANDARD' CHECK (spot_type IN ('STANDARD', 'DISABLED', 'EV', 'LARGE')),
    status VARCHAR2(20) DEFAULT 'AVAILABLE' CHECK (status IN ('AVAILABLE', 'OCCUPIED', 'RESERVED', 'MAINTENANCE')),
    hourly_rate NUMBER(8, 2) NOT NULL CHECK (hourly_rate >= 0)
);

CREATE TABLE reservations (
    reservation_id NUMBER PRIMARY KEY,
    user_id NUMBER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    spot_id NUMBER NOT NULL REFERENCES spots(spot_id) ON DELETE CASCADE,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP NOT NULL,
    status VARCHAR2(20) DEFAULT 'CONFIRMED' CHECK (status IN ('CONFIRMED', 'CANCELLED', 'COMPLETED')),
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
    CONSTRAINT chk_reserve_time CHECK (end_time > start_time)
);

CREATE TABLE parking_sessions (
    session_id NUMBER PRIMARY KEY,
    user_id NUMBER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    spot_id NUMBER NOT NULL REFERENCES spots(spot_id) ON DELETE CASCADE,
    vehicle_plate VARCHAR2(20) NOT NULL,
    entry_time TIMESTAMP DEFAULT SYSTIMESTAMP,
    exit_time TIMESTAMP,
    duration_hours NUMBER(10, 2),
    total_cost NUMBER(10, 2),
    session_status VARCHAR2(20) DEFAULT 'ACTIVE' CHECK (session_status IN ('ACTIVE', 'COMPLETED', 'CANCELLED')),
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP
);

CREATE TABLE parking_audit_log (
    log_id NUMBER PRIMARY KEY,
    action VARCHAR2(50),
    table_name VARCHAR2(50),
    record_id NUMBER,
    user_id NUMBER,
    timestamp_log TIMESTAMP DEFAULT SYSTIMESTAMP,
    details VARCHAR2(1000)
);

CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_spots_status ON spots(status);
CREATE INDEX idx_reservations_user ON reservations(user_id);
CREATE INDEX idx_reservations_spot ON reservations(spot_id);
CREATE INDEX idx_reservations_status ON reservations(status);
CREATE INDEX idx_sessions_user ON parking_sessions(user_id);
CREATE INDEX idx_sessions_spot ON parking_sessions(spot_id);
CREATE INDEX idx_sessions_status ON parking_sessions(session_status);

CREATE OR REPLACE TRIGGER trg_users_pk
BEFORE INSERT ON users
FOR EACH ROW
BEGIN
    IF :NEW.user_id IS NULL THEN
        :NEW.user_id := seq_user_id.NEXTVAL;
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_spots_pk
BEFORE INSERT ON spots
FOR EACH ROW
BEGIN
    IF :NEW.spot_id IS NULL THEN
        :NEW.spot_id := seq_spot_id.NEXTVAL;
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_reservations_pk
BEFORE INSERT ON reservations
FOR EACH ROW
BEGIN
    IF :NEW.reservation_id IS NULL THEN
        :NEW.reservation_id := seq_reservation_id.NEXTVAL;
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_sessions_pk
BEFORE INSERT ON parking_sessions
FOR EACH ROW
BEGIN
    IF :NEW.session_id IS NULL THEN
        :NEW.session_id := seq_session_id.NEXTVAL;
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_audit_pk
BEFORE INSERT ON parking_audit_log
FOR EACH ROW
BEGIN
    IF :NEW.log_id IS NULL THEN
        :NEW.log_id := seq_audit_id.NEXTVAL;
    END IF;
END;
/

CREATE OR REPLACE FUNCTION calculate_duration(
    p_entry_time TIMESTAMP,
    p_exit_time TIMESTAMP
) RETURN NUMBER IS
    v_duration NUMBER(10, 2);
    v_exit_time TIMESTAMP;
BEGIN
    v_exit_time := p_exit_time;

    IF v_exit_time IS NULL THEN
        v_exit_time := SYSTIMESTAMP;
    END IF;

    IF v_exit_time < p_entry_time THEN
        RAISE_APPLICATION_ERROR(-20001, 'Exit time cannot be before entry time');
    END IF;

    v_duration := ROUND((CAST(v_exit_time AS DATE) - CAST(p_entry_time AS DATE)) * 24, 2);
    RETURN v_duration;
EXCEPTION
    WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE('Error calculating duration: ' || SQLERRM);
        RETURN 0;
END calculate_duration;
/

CREATE OR REPLACE FUNCTION calculate_parking_cost(
    p_spot_id NUMBER,
    p_user_id NUMBER,
    p_duration NUMBER
) RETURN NUMBER IS
    v_hourly_rate NUMBER(8, 2);
    v_user_type VARCHAR2(20);
    v_discount_rate NUMBER(3, 2) := 1.00;
    v_total_cost NUMBER(10, 2);
    v_spot_count NUMBER;
    v_user_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_spot_count
    FROM spots
    WHERE spot_id = p_spot_id;

    IF v_spot_count = 0 THEN
        RAISE_APPLICATION_ERROR(-20002, 'Spot ID ' || p_spot_id || ' not found');
    END IF;

    SELECT COUNT(*) INTO v_user_count
    FROM users
    WHERE user_id = p_user_id AND is_active = 'Y';

    IF v_user_count = 0 THEN
        RAISE_APPLICATION_ERROR(-20003, 'User ID ' || p_user_id || ' not found or inactive');
    END IF;

    SELECT hourly_rate INTO v_hourly_rate
    FROM spots
    WHERE spot_id = p_spot_id;

    SELECT user_type INTO v_user_type
    FROM users
    WHERE user_id = p_user_id;

    CASE v_user_type
        WHEN 'PREMIUM' THEN v_discount_rate := 0.90;
        WHEN 'VIP' THEN v_discount_rate := 0.80;
        ELSE v_discount_rate := 1.00;
    END CASE;

    v_total_cost := v_hourly_rate * p_duration * v_discount_rate;
    RETURN ROUND(v_total_cost, 2);
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        DBMS_OUTPUT.PUT_LINE('Data not found for spot or user');
        RETURN 0;
    WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE('Error calculating cost: ' || SQLERRM);
        RETURN 0;
END calculate_parking_cost;
/

CREATE OR REPLACE FUNCTION check_spot_availability(
    p_spot_id NUMBER
) RETURN NUMBER IS
    v_status spots.status%TYPE;
BEGIN
    SELECT status INTO v_status
    FROM spots
    WHERE spot_id = p_spot_id;

    IF v_status = 'AVAILABLE' THEN
        RETURN 1;
    END IF;

    RETURN 0;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        RETURN 0;
    WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE('Error checking availability: ' || SQLERRM);
        RETURN 0;
END check_spot_availability;
/

CREATE OR REPLACE FUNCTION get_user_stats_json(
    p_user_id NUMBER
) RETURN VARCHAR2 IS
    CURSOR v_cursor IS
        SELECT session_id, total_cost, duration_hours, created_at
        FROM parking_sessions
        WHERE user_id = p_user_id AND session_status = 'COMPLETED';

    v_total_sessions NUMBER := 0;
    v_total_spent NUMBER := 0;
    v_total_duration NUMBER := 0;
    v_last_date TIMESTAMP;
    v_avg_duration NUMBER := 0;
    v_result VARCHAR2(1000);
BEGIN
    FOR rec IN v_cursor LOOP
        v_total_sessions := v_total_sessions + 1;
        v_total_spent := v_total_spent + NVL(rec.total_cost, 0);
        v_total_duration := v_total_duration + NVL(rec.duration_hours, 0);
        v_last_date := rec.created_at;
    END LOOP;

    IF v_total_sessions > 0 THEN
        v_avg_duration := v_total_duration / v_total_sessions;
    END IF;

    v_result := '{"total_sessions":' || v_total_sessions ||
                ',"total_spent":' || v_total_spent ||
                ',"avg_duration":' || ROUND(v_avg_duration, 2) ||
                ',"last_parking_date":"' || TO_CHAR(v_last_date, 'YYYY-MM-DD HH24:MI:SS') || '"}';

    RETURN v_result;
EXCEPTION
    WHEN OTHERS THEN
        DBMS_OUTPUT.PUT_LINE('Error getting user stats: ' || SQLERRM);
        RETURN '{"total_sessions":0,"total_spent":0,"avg_duration":0,"last_parking_date":null}';
END get_user_stats_json;
/

CREATE OR REPLACE PROCEDURE start_parking_session(
    p_user_id IN NUMBER,
    p_spot_id IN NUMBER,
    p_vehicle_plate IN VARCHAR2,
    p_session_id OUT NUMBER,
    p_message OUT VARCHAR2
) IS
    v_spot_available NUMBER;
    v_user_exists NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_user_exists
    FROM users
    WHERE user_id = p_user_id AND is_active = 'Y';

    IF v_user_exists = 0 THEN
        RAISE_APPLICATION_ERROR(-20011, 'USER_NOT_FOUND_OR_INACTIVE');
    END IF;

    v_spot_available := check_spot_availability(p_spot_id);

    IF v_spot_available = 0 THEN
        RAISE_APPLICATION_ERROR(-20010, 'SPOT_NOT_AVAILABLE');
    END IF;

    INSERT INTO parking_sessions (user_id, spot_id, vehicle_plate, session_status)
    VALUES (p_user_id, p_spot_id, p_vehicle_plate, 'ACTIVE')
    RETURNING session_id INTO p_session_id;

    UPDATE spots
    SET status = 'OCCUPIED'
    WHERE spot_id = p_spot_id;

    p_message := 'Session started successfully';
    COMMIT;
EXCEPTION
    WHEN OTHERS THEN
        p_session_id := NULL;
        p_message := SQLERRM;
        ROLLBACK;
END start_parking_session;
/

CREATE OR REPLACE PROCEDURE end_parking_session(
    p_session_id IN NUMBER,
    p_total_cost OUT NUMBER,
    p_message OUT VARCHAR2
) IS
    v_entry_time TIMESTAMP;
    v_spot_id NUMBER;
    v_user_id NUMBER;
    v_duration NUMBER;
    v_cost NUMBER;
    v_session_status VARCHAR2(20);
BEGIN
    SELECT entry_time, spot_id, user_id, session_status
    INTO v_entry_time, v_spot_id, v_user_id, v_session_status
    FROM parking_sessions
    WHERE session_id = p_session_id;

    IF v_session_status != 'ACTIVE' THEN
        RAISE_APPLICATION_ERROR(-20020, 'Session is not active');
    END IF;

    v_duration := calculate_duration(v_entry_time, SYSTIMESTAMP);
    v_cost := calculate_parking_cost(v_spot_id, v_user_id, v_duration);

    UPDATE parking_sessions
    SET exit_time = SYSTIMESTAMP,
        duration_hours = v_duration,
        total_cost = v_cost,
        session_status = 'COMPLETED'
    WHERE session_id = p_session_id;

    UPDATE spots
    SET status = 'AVAILABLE'
    WHERE spot_id = v_spot_id;

    p_total_cost := v_cost;
    p_message := 'Session ended successfully';
    COMMIT;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        p_total_cost := NULL;
        p_message := 'Session not found';
        ROLLBACK;
    WHEN OTHERS THEN
        p_total_cost := NULL;
        p_message := SQLERRM;
        ROLLBACK;
END end_parking_session;
/

CREATE OR REPLACE PROCEDURE create_reservation(
    p_user_id IN NUMBER,
    p_spot_id IN NUMBER,
    p_start_time IN TIMESTAMP,
    p_end_time IN TIMESTAMP,
    p_reservation_id OUT NUMBER,
    p_message OUT VARCHAR2
) IS
BEGIN
    IF p_end_time <= p_start_time THEN
        RAISE_APPLICATION_ERROR(-20030, 'Reservation end_time must be greater than start_time');
    END IF;

    INSERT INTO reservations (user_id, spot_id, start_time, end_time, status)
    VALUES (p_user_id, p_spot_id, p_start_time, p_end_time, 'CONFIRMED')
    RETURNING reservation_id INTO p_reservation_id;

    p_message := 'Reservation created successfully';
    COMMIT;
EXCEPTION
    WHEN OTHERS THEN
        p_reservation_id := NULL;
        p_message := SQLERRM;
        ROLLBACK;
END create_reservation;
/

CREATE OR REPLACE PROCEDURE generate_park_report(
    p_scope_id IN NUMBER,
    p_total_revenue OUT NUMBER,
    p_total_sessions OUT NUMBER,
    p_avg_duration OUT NUMBER,
    p_message OUT VARCHAR2
) IS
    CURSOR v_cursor IS
        SELECT total_cost, duration_hours
        FROM parking_sessions
        WHERE session_status = 'COMPLETED';

    v_revenue NUMBER := 0;
    v_sessions NUMBER := 0;
    v_total_duration NUMBER := 0;
BEGIN
    FOR rec IN v_cursor LOOP
        v_revenue := v_revenue + NVL(rec.total_cost, 0);
        v_total_duration := v_total_duration + NVL(rec.duration_hours, 0);
        v_sessions := v_sessions + 1;
    END LOOP;

    p_total_revenue := v_revenue;
    p_total_sessions := v_sessions;

    IF v_sessions > 0 THEN
        p_avg_duration := v_total_duration / v_sessions;
    ELSE
        p_avg_duration := 0;
    END IF;

    p_message := 'Report generated successfully';
EXCEPTION
    WHEN OTHERS THEN
        p_total_revenue := 0;
        p_total_sessions := 0;
        p_avg_duration := 0;
        p_message := SQLERRM;
END generate_park_report;
/

CREATE OR REPLACE TRIGGER trg_update_user_timestamp
BEFORE UPDATE ON users
FOR EACH ROW
BEGIN
    :NEW.updated_at := SYSTIMESTAMP;
END;
/

CREATE OR REPLACE TRIGGER trg_prevent_active_delete
BEFORE DELETE ON parking_sessions
FOR EACH ROW
BEGIN
    IF :OLD.session_status = 'ACTIVE' THEN
        RAISE_APPLICATION_ERROR(-20110, 'Cannot delete active parking session. End the session first.');
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_reservation_no_overlap
BEFORE INSERT OR UPDATE OF start_time, end_time, status, spot_id ON reservations
FOR EACH ROW
DECLARE
    v_conflicts NUMBER;
BEGIN
    IF :NEW.status = 'CONFIRMED' THEN
        SELECT COUNT(*)
          INTO v_conflicts
          FROM reservations r
         WHERE r.spot_id = :NEW.spot_id
           AND r.status = 'CONFIRMED'
           AND (:NEW.start_time < r.end_time AND :NEW.end_time > r.start_time)
           AND (r.reservation_id != NVL(:NEW.reservation_id, -1));

        IF v_conflicts > 0 THEN
            RAISE_APPLICATION_ERROR(-20120, 'Reservation conflicts with an existing confirmed reservation');
        END IF;
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_log_parking_session
AFTER INSERT OR UPDATE OR DELETE ON parking_sessions
FOR EACH ROW
BEGIN
    IF INSERTING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('INSERT', 'parking_sessions', :NEW.session_id, :NEW.user_id,
                'New parking session for vehicle: ' || :NEW.vehicle_plate || ', spot_id=' || :NEW.spot_id);
    ELSIF UPDATING AND :OLD.session_status != :NEW.session_status THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('UPDATE', 'parking_sessions', :NEW.session_id, :NEW.user_id,
                'Session status changed from ' || :OLD.session_status || ' to ' || :NEW.session_status);
    ELSIF DELETING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('DELETE', 'parking_sessions', :OLD.session_id, :OLD.user_id,
                'Parking session deleted for vehicle: ' || :OLD.vehicle_plate || ', spot_id=' || :OLD.spot_id);
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_log_users
AFTER INSERT OR UPDATE OR DELETE ON users
FOR EACH ROW
BEGIN
    IF INSERTING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('INSERT', 'users', :NEW.user_id, :NEW.user_id,
                'User created: username=' || :NEW.username || ', role=' || :NEW.role || ', status=' || :NEW.is_active);
    ELSIF UPDATING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('UPDATE', 'users', :NEW.user_id, :NEW.user_id,
                'User updated: username=' || :NEW.username || ', role=' || :NEW.role || ', status=' || :NEW.is_active);
    ELSIF DELETING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('DELETE', 'users', :OLD.user_id, :OLD.user_id,
                'User deleted: username=' || :OLD.username || ', role=' || :OLD.role);
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_log_spots
AFTER INSERT OR UPDATE OR DELETE ON spots
FOR EACH ROW
BEGIN
    IF INSERTING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('INSERT', 'spots', :NEW.spot_id, NULL,
                'Spot created: number=' || :NEW.spot_number || ', type=' || :NEW.spot_type || ', status=' || :NEW.status);
    ELSIF UPDATING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('UPDATE', 'spots', :NEW.spot_id, NULL,
                'Spot updated: number=' || :NEW.spot_number || ', status=' || :OLD.status || ' -> ' || :NEW.status);
    ELSIF DELETING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('DELETE', 'spots', :OLD.spot_id, NULL,
                'Spot deleted: number=' || :OLD.spot_number || ', status=' || :OLD.status);
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_log_reservations
AFTER INSERT OR UPDATE OR DELETE ON reservations
FOR EACH ROW
BEGIN
    IF INSERTING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('INSERT', 'reservations', :NEW.reservation_id, :NEW.user_id,
                'Reservation created for spot_id=' || :NEW.spot_id || ', status=' || :NEW.status);
    ELSIF UPDATING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('UPDATE', 'reservations', :NEW.reservation_id, :NEW.user_id,
                'Reservation updated: spot_id=' || :NEW.spot_id || ', status=' || :OLD.status || ' -> ' || :NEW.status);
    ELSIF DELETING THEN
        INSERT INTO parking_audit_log (action, table_name, record_id, user_id, details)
        VALUES ('DELETE', 'reservations', :OLD.reservation_id, :OLD.user_id,
                'Reservation deleted for spot_id=' || :OLD.spot_id || ', status=' || :OLD.status);
    END IF;
END;
/

CREATE OR REPLACE TRIGGER trg_sync_spot_status
AFTER INSERT OR UPDATE OF session_status ON parking_sessions
FOR EACH ROW
BEGIN
    IF INSERTING AND :NEW.session_status = 'ACTIVE' THEN
        UPDATE spots
        SET status = 'OCCUPIED'
        WHERE spot_id = :NEW.spot_id;
    ELSIF UPDATING AND :OLD.session_status = 'ACTIVE' AND :NEW.session_status != 'ACTIVE' THEN
        UPDATE spots
        SET status = 'AVAILABLE'
        WHERE spot_id = :NEW.spot_id;
    END IF;
END;
/

INSERT INTO users (username, email, phone, user_type) VALUES
    ('john_doe', 'john@example.com', '+1234567890', 'REGULAR');
INSERT INTO users (username, email, phone, user_type) VALUES
    ('jane_smith', 'jane@example.com', '+1234567891', 'PREMIUM');
INSERT INTO users (username, email, phone, user_type) VALUES
    ('bob_wilson', 'bob@example.com', '+1234567892', 'VIP');
INSERT INTO users (username, email, phone, user_type) VALUES
    ('alice_brown', 'alice@example.com', '+1234567893', 'REGULAR');
INSERT INTO users (username, email, phone, user_type, password_hash, role, money_balance) VALUES
    ('admin', 'admin@parking.local', '+1000000000', 'REGULAR', 'pbkdf2_sha256$120000$parking-oracle-admin-salt$5a133e412b130533b66f767eacd5bf070835977e2399cc1860ed819a24b537d0', 'ADMIN', 100.00  );

INSERT INTO spots (spot_id, spot_number, spot_type, status, hourly_rate) VALUES (1, 'A-101', 'STANDARD', 'AVAILABLE', 5.00);
INSERT INTO spots (spot_id, spot_number, spot_type, status, hourly_rate) VALUES (2, 'A-102', 'STANDARD', 'AVAILABLE', 5.00);
INSERT INTO spots (spot_id, spot_number, spot_type, status, hourly_rate) VALUES (3, 'A-103', 'DISABLED', 'AVAILABLE', 5.00);
INSERT INTO spots (spot_id, spot_number, spot_type, status, hourly_rate) VALUES (4, 'A-104', 'EV', 'AVAILABLE', 5.00);
INSERT INTO spots (spot_id, spot_number, spot_type, status, hourly_rate) VALUES (5, 'A-105', 'LARGE', 'AVAILABLE', 5.00);
INSERT INTO spots (spot_id, spot_number, spot_type, status, hourly_rate) VALUES (6, 'A-106', 'STANDARD', 'AVAILABLE', 5.00);
INSERT INTO spots (spot_id, spot_number, spot_type, status, hourly_rate) VALUES (7, 'A-107', 'EV', 'AVAILABLE', 6.00);
INSERT INTO spots (spot_id, spot_number, spot_type, status, hourly_rate) VALUES (8, 'A-108', 'DISABLED', 'AVAILABLE', 5.00);

COMMIT;
