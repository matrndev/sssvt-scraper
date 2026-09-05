import psycopg


SCHEMA = """
CREATE TABLE IF NOT EXISTS teachers (
    abbrev VARCHAR(2) PRIMARY KEY,
    name VARCHAR(100)
);
CREATE TABLE IF NOT EXISTS subjects (
    abbrev VARCHAR(10) PRIMARY KEY,
    name VARCHAR(100)
);
CREATE TABLE IF NOT EXISTS classes (
    code VARCHAR(10) PRIMARY KEY,
    class_teacher VARCHAR(2) REFERENCES teachers(abbrev),
    home_classroom VARCHAR(20)
);
CREATE TABLE IF NOT EXISTS timetable (
    id BIGSERIAL PRIMARY KEY,
    class VARCHAR(10) NOT NULL REFERENCES classes(code),
    weekday SMALLINT NOT NULL CHECK (weekday BETWEEN 1 AND 5),
    period SMALLINT NOT NULL CHECK (period > 0),
    subject VARCHAR(10) NOT NULL REFERENCES subjects(abbrev),
    teacher VARCHAR(2) REFERENCES teachers(abbrev),
    room VARCHAR(20),
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
    old_room VARCHAR(20),
    new_subject VARCHAR(10) REFERENCES subjects(abbrev),
    new_teacher VARCHAR(2) REFERENCES teachers(abbrev),
    new_room VARCHAR(20),
    note TEXT
);
"""


def save_snapshot(snapshot, database_url):
    with psycopg.connect(database_url, connect_timeout=30) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute("TRUNCATE TABLE substitutions, timetable, classes, subjects, teachers RESTART IDENTITY")
            cursor.executemany("INSERT INTO teachers (abbrev, name) VALUES (%s, %s)", snapshot["teachers"])
            cursor.executemany("INSERT INTO subjects (abbrev, name) VALUES (%s, %s)", snapshot["subjects"])
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
