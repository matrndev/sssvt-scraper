import psycopg


SCHEMA = """
CREATE TABLE IF NOT EXISTS system (
    scrape_type VARCHAR PRIMARY KEY,
    last_scrape TIMESTAMPTZ NOT NULL,
    scrape_success BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS teachers (
    abbrev VARCHAR(2) PRIMARY KEY,
    name VARCHAR(100)
);
ALTER TABLE teachers ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE teachers ADD COLUMN IF NOT EXISTS room VARCHAR(20);
ALTER TABLE teachers ADD COLUMN IF NOT EXISTS phone_number TEXT;
CREATE TABLE IF NOT EXISTS consultation (
    id BIGSERIAL PRIMARY KEY,
    teacher VARCHAR(2) NOT NULL UNIQUE REFERENCES teachers(abbrev),
    hours TEXT,
    room VARCHAR(20)
);
ALTER TABLE teachers ADD COLUMN IF NOT EXISTS consultation BIGINT UNIQUE REFERENCES consultation(id);
CREATE TABLE IF NOT EXISTS subjects (
    abbrev VARCHAR(10) PRIMARY KEY,
    name VARCHAR(100)
);
CREATE TABLE IF NOT EXISTS classes (
    code VARCHAR(10) PRIMARY KEY,
    class_teacher VARCHAR(2) REFERENCES teachers(abbrev),
    home_classroom VARCHAR(20)
);
CREATE TABLE IF NOT EXISTS rooms (
    id VARCHAR(20) PRIMARY KEY,
    is_computer_room BOOLEAN
);
CREATE TABLE IF NOT EXISTS timetable (
    id BIGSERIAL PRIMARY KEY,
    class VARCHAR(10) NOT NULL REFERENCES classes(code),
    weekday SMALLINT NOT NULL CHECK (weekday BETWEEN 1 AND 5),
    period SMALLINT NOT NULL CHECK (period > 0),
    subject VARCHAR(10) NOT NULL REFERENCES subjects(abbrev),
    teacher VARCHAR(2) REFERENCES teachers(abbrev),
    room VARCHAR(20) REFERENCES rooms(id),
    group_num SMALLINT NULL CHECK (group_num IS NULL OR group_num > 0)
);
CREATE TABLE IF NOT EXISTS substitutions (
    id BIGSERIAL PRIMARY KEY,
    class VARCHAR(10) NOT NULL REFERENCES classes(code),
    weekday SMALLINT NOT NULL CHECK (weekday BETWEEN 1 AND 5),
    period SMALLINT NOT NULL CHECK (period > 0),
    group_num SMALLINT NULL CHECK (group_num IS NULL OR group_num > 0),
    change_type VARCHAR(20) NOT NULL,
    old_subject VARCHAR(10) REFERENCES subjects(abbrev),
    old_teacher VARCHAR(2) REFERENCES teachers(abbrev),
    old_room VARCHAR(20) REFERENCES rooms(id),
    new_subject VARCHAR(10) REFERENCES subjects(abbrev),
    new_teacher VARCHAR(2) REFERENCES teachers(abbrev),
    new_room VARCHAR(20) REFERENCES rooms(id),
    note TEXT
);
INSERT INTO rooms (id)
SELECT room FROM timetable WHERE room IS NOT NULL
UNION SELECT old_room FROM substitutions WHERE old_room IS NOT NULL
UNION SELECT new_room FROM substitutions WHERE new_room IS NOT NULL
ON CONFLICT (id) DO NOTHING;
DO $$
DECLARE
    ref RECORD;
BEGIN
    FOR ref IN SELECT * FROM (VALUES
        ('timetable', 'room', 'timetable_room_fkey'),
        ('substitutions', 'old_room', 'substitutions_old_room_fkey'),
        ('substitutions', 'new_room', 'substitutions_new_room_fkey')
    ) AS refs(table_name, column_name, constraint_name)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = ref.table_name::regclass AND conname = ref.constraint_name
        ) THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES rooms(id)',
                ref.table_name, ref.constraint_name, ref.column_name);
        END IF;
    END LOOP;
END $$;
"""


def save_scrape_status(database_url, scrape_type, scrape_success):
    with psycopg.connect(database_url, connect_timeout=30) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute(
                """INSERT INTO system (scrape_type, last_scrape, scrape_success)
                   VALUES (%s, CURRENT_TIMESTAMP, %s)
                   ON CONFLICT (scrape_type) DO UPDATE SET
                       last_scrape = EXCLUDED.last_scrape,
                       scrape_success = EXCLUDED.scrape_success""",
                (scrape_type, scrape_success),
            )


def save_snapshot(snapshot, database_url):
    with psycopg.connect(database_url, connect_timeout=30) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute("TRUNCATE TABLE substitutions, timetable, classes RESTART IDENTITY")
            rooms = {row[5] for row in snapshot["timetable"]}
            rooms.update(row[index] for row in snapshot["substitutions"] for index in (7, 10))
            cursor.executemany(
                "INSERT INTO rooms (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                [(room,) for room in sorted(rooms - {None})],
            )
            cursor.executemany(
                """INSERT INTO teachers (abbrev, name) VALUES (%s, %s)
                   ON CONFLICT (abbrev) DO UPDATE SET name = COALESCE(EXCLUDED.name, teachers.name)""",
                snapshot["teachers"],
            )
            cursor.executemany(
                "INSERT INTO subjects (abbrev, name) VALUES (%s, %s) ON CONFLICT (abbrev) DO NOTHING",
                snapshot["subjects"],
            )
            cursor.executemany("INSERT INTO classes (code, class_teacher, home_classroom) VALUES (%s, %s, %s)", snapshot["classes"])
            cursor.executemany(
                "INSERT INTO timetable (class, weekday, period, subject, teacher, room, group_num) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                snapshot["timetable"],
            )
            cursor.executemany(
                """INSERT INTO substitutions
                   (class, weekday, period, group_num, change_type, old_subject, old_teacher, old_room, new_subject, new_teacher, new_room, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                snapshot["substitutions"],
            )


def save_contacts(contacts, database_url):
    if not contacts or len({contact["abbrev"] for contact in contacts}) != len(contacts):
        raise ValueError("Missing or duplicate teacher contacts")
    with psycopg.connect(database_url, connect_timeout=30) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            for contact in contacts:
                cursor.execute(
                    """INSERT INTO teachers (abbrev, name, email, room, phone_number)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (abbrev) DO UPDATE SET
                           name = EXCLUDED.name, email = EXCLUDED.email,
                           room = EXCLUDED.room, phone_number = EXCLUDED.phone_number""",
                    (contact["abbrev"], contact["name"], contact["email"], contact["room"], contact["phone_number"]),
                )
                cursor.execute(
                    """INSERT INTO consultation (teacher, hours, room) VALUES (%s, %s, %s)
                       ON CONFLICT (teacher) DO UPDATE SET hours = EXCLUDED.hours, room = EXCLUDED.room
                       RETURNING id""",
                    (contact["abbrev"], contact["consultation"]["hours"], contact["consultation"]["room"]),
                )
                consultation_id = cursor.fetchone()[0]
                cursor.execute(
                    "UPDATE teachers SET consultation = %s WHERE abbrev = %s",
                    (consultation_id, contact["abbrev"]),
                )
