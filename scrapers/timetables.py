import argparse
import logging
import os
import re
from copy import deepcopy
from urllib.parse import quote

import scrapy
from scrapy.crawler import CrawlerProcess

from scrapers.logging_setup import logged_run

from database import save_scrape_status, save_snapshot

from dotenv import load_dotenv
load_dotenv()


SOURCE_URL = "https://www.sssvt.cz/IS/rozvrh-hodin/suplovaci/"
REGULAR_URL = "https://www.sssvt.cz/IS/rozvrh-hodin/"
WEEKDAYS = {"Po": 1, "Út": 2, "St": 3, "Čt": 4, "Pá": 5}
GROUP_PARTNERS = {1: 2, 2: 1, 3: 4, 4: 3}


def text(selector):
    return " ".join(selector.xpath("string(.)").get(default="").split())


def parse_period(hour):
    number = text(hour.css("strong"))
    if not number.isdigit() or not 0 < int(number) <= 32767:
        raise ValueError("Unrecognized lesson period header")
    return {"number": int(number)}


def parse_group(value):
    if not value:
        return None
    match = re.fullmatch(r"(?:([1-9]\d*)\.sk|\(([1-9]\d*)\.sk\))", value)
    if not match or int(match[1] or match[2]) > 32767:
        raise ValueError(f"Unrecognized lesson group: {value!r}")
    return int(match[1] or match[2])


def parse_lesson(group, group_selector=".classGroup"):
    classes = group.attrib.get("class", "").split()
    teacher = group.css(".teacher a")
    code = text(teacher)
    subject = text(group.css("strong"))
    if not subject:
        raise ValueError("Lesson is missing its subject")
    for selector in (".teacher", ".room"):
        element = group.css(selector)
        if len(element.css("a")) > 1 or (text(element) and not element.css("a")):
            raise ValueError(f"Unrecognized {selector} layout in lesson")
    return {
        "subject": subject,
        "subject_name": group.css("strong").attrib.get("title", "").strip() or None,
        "group": parse_group(text(group.css(group_selector))),
        "teacher": {
            "code": code,
            "name": teacher.attrib.get("title", "").strip() or None,
        } if code else None,
        "room": text(group.css(".room a")) or None,
        "changed": "zmena" in classes,
        "special": "special" in classes,
        "text": text(group),
    }


def parse_lessons(hour):
    lessons = [parse_lesson(group) for group in hour.xpath("./div")]
    if len(lessons) == 2:
        for lunch, other in (lessons, lessons[::-1]):
            if lunch["subject"] == "oběd" and lunch["group"] is None and other["subject"] != "oběd":
                lunch["group"] = GROUP_PARTNERS.get(other["group"])
    return lessons


def parse_timetable(table):
    heading = text(table.xpath("preceding-sibling::h2[1]"))
    if not heading.startswith("Rozvrh třídy "):
        raise ValueError(f"Unrecognized timetable heading: {heading!r}")
    details = table.xpath("preceding-sibling::p[1]/strong")
    if len(details) != 2 or not all(text(detail) for detail in details):
        raise ValueError(f"Missing class teacher or home classroom in {heading}")
    periods = [parse_period(hour) for hour in table.css(".header > .hour") if hour.css("strong")]
    if not periods or len({period["number"] for period in periods}) != len(periods):
        raise ValueError(f"Missing or duplicate periods in {heading}")
    days = []
    for day in table.css(".day"):
        label = text(day.css(".dayTitle"))
        hours = day.css(".day > .hour:not(.dayTitle)")
        if label not in WEEKDAYS:
            raise ValueError(f"Unrecognized weekday: {label!r}")
        if len(hours) != len(periods):
            raise ValueError(f"Unexpected period count in {heading}, {label}")
        for hour in hours:
            groups = hour.xpath("./div")
            expected = re.search(r"\bgroup(\d+)\b", hour.attrib.get("class", ""))
            if not expected or len(groups) != int(expected[1]):
                raise ValueError(f"Unexpected lesson count in {heading}, {label}")
            if (not groups and text(hour)) or any("group" not in group.attrib.get("class", "").split() for group in groups):
                raise ValueError(f"Unrecognized lesson layout in {heading}, {label}")
        days.append({
            "day": label,
            "periods": [
                {
                    "number": period["number"],
                    "lessons": parse_lessons(hour),
                }
                for period, hour in zip(periods, hours)
            ],
        })
    if len(days) != 5 or {day["day"] for day in days} != set(WEEKDAYS):
        raise ValueError(f"Missing or duplicate weekdays in {heading}")
    return {
        "class": heading.removeprefix("Rozvrh třídy "),
        "class_teacher": text(details[0]) if details else None,
        "homeroom": text(details[1]) if len(details) > 1 else None,
        "periods": periods,
        "days": days,
    }


def parse_page(response):
    if response.url not in (SOURCE_URL, REGULAR_URL):
        raise ValueError(f"Unexpected source URL: {response.url}")
    timetables = [parse_timetable(table) for table in response.css(".timetable.tt-class")]
    if not timetables:
        raise ValueError("No class timetables found; the page layout may have changed")
    names = [table["class"] for table in timetables]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate class timetables found")
    advertised = {text(link) for link in response.css('p.links a[href*="/class/"]')}
    if not advertised or advertised != set(names):
        raise ValueError("Class timetables do not match the page's class list")
    teachers = [
        {"code": text(link), "name": link.attrib.get("title", "").strip() or None}
        for link in response.css('p.links a[href*="/teacher/"]')
    ]
    return {"timetables": timetables, "teachers": teachers}


def parse_room_page(response, room):
    tables = response.css(".timetable.tt-room")
    if len(tables) != 1 or text(tables.xpath("preceding-sibling::h2[1]")) != f"Rozvrh místnosti {room}":
        raise ValueError(f"Missing or unexpected room timetable: {room}")
    table = tables[0]
    periods = [parse_period(hour)["number"] for hour in table.css(".header > .hour") if hour.css("strong")]
    if not periods or len(set(periods)) != len(periods):
        raise ValueError(f"Missing or duplicate room periods: {room}")
    slots = {}
    days = table.css(".day")
    if len(days) != 5 or {text(day.css('.dayTitle')) for day in days} != set(WEEKDAYS):
        raise ValueError(f"Missing or duplicate room weekdays: {room}")
    for day in days:
        weekday = WEEKDAYS[text(day.css(".dayTitle"))]
        hours = day.css(".day > .hour:not(.dayTitle)")
        if len(hours) != len(periods):
            raise ValueError(f"Unexpected room period count: {room}")
        for period, hour in zip(periods, hours):
            groups = hour.xpath("./div")
            expected = re.search(r"\bgroup(\d+)\b", hour.attrib.get("class", ""))
            if not expected or len(groups) != int(expected[1]) or (not groups and text(hour)):
                raise ValueError(f"Unexpected room lesson count: {room}")
            lessons = set()
            for group in groups:
                code = text(group.css(".class a"))
                subject = text(group.css("strong"))
                if "group" not in group.attrib.get("class", "").split() or not code or not subject:
                    raise ValueError(f"Unrecognized room lesson layout: {room}")
                lessons.add((code, parse_group(text(group.css(".classGroup > .group"))),
                             subject, text(group.css(".teacher a")) or None))
            slots[weekday, period] = lessons
    return slots


def parse_teacher_page(response, teacher):
    tables = response.css(".timetable.tt-teacher")
    heading = text(tables.xpath("preceding-sibling::h2[1]"))
    prefix = "Rozvrh vyučujícího "
    if (len(tables) != 1 or not heading.startswith(prefix) or not teacher["name"]
            or teacher_name_key(heading.removeprefix(prefix)) != teacher_name_key(teacher["name"])):
        raise ValueError(f"Missing or unexpected teacher timetable: {teacher['code']}")
    table = tables[0]
    periods = [parse_period(hour)["number"] for hour in table.css(".header > .hour") if hour.css("strong")]
    if not periods or len(set(periods)) != len(periods):
        raise ValueError(f"Missing or duplicate teacher periods: {teacher['code']}")
    days = table.css(".day")
    if len(days) != 5 or {text(day.css('.dayTitle')) for day in days} != set(WEEKDAYS):
        raise ValueError(f"Missing or duplicate teacher weekdays: {teacher['code']}")
    slots = {}
    for day in days:
        weekday = WEEKDAYS[text(day.css(".dayTitle"))]
        hours = day.css(".day > .hour:not(.dayTitle)")
        if len(hours) != len(periods):
            raise ValueError(f"Unexpected teacher period count: {teacher['code']}")
        for period, hour in zip(periods, hours):
            groups = hour.xpath("./div")
            expected = re.search(r"\bgroup(\d+)\b", hour.attrib.get("class", ""))
            if not expected or len(groups) != int(expected[1]) or (not groups and text(hour)):
                raise ValueError(f"Unexpected teacher lesson count: {teacher['code']}")
            lessons = []
            for group in groups:
                classes = group.attrib.get("class", "").split()
                if "group" not in classes:
                    raise ValueError(f"Unrecognized teacher lesson layout: {teacher['code']}")
                if {"special", "zmena"} <= set(classes) and not text(group):
                    continue
                code = text(group.css(".class a"))
                if not code or len(group.css(".class a")) != 1:
                    raise ValueError(f"Unrecognized teacher lesson class: {teacher['code']}")
                lesson = parse_lesson(group, ".classGroup > .group")
                lesson.update({"class": code, "teacher": dict(teacher)})
                lessons.append(lesson)
            slots[weekday, period] = lessons
    return slots


def reconcile_teachers(regular, current, teachers):
    advertised = {teacher["code"] for teacher in current["teachers"]}
    if not advertised or teachers.keys() != advertised:
        raise ValueError("Incomplete teacher scrape")
    result = deepcopy(current)
    current_slots = {
        (table["class"], WEEKDAYS[day["day"]], period["number"]): period
        for table in result["timetables"] for day in table["days"] for period in day["periods"]
    }
    regular_slots = {
        (table["class"], WEEKDAYS[day["day"]], period["number"]): period["lessons"]
        for table in regular["timetables"] for day in table["days"] for period in day["periods"]
    }
    expected_slots = {key[1:] for key in current_slots}
    assignments = {key: [] for key in current_slots}
    for teacher, slots in teachers.items():
        if slots.keys() != expected_slots:
            raise ValueError(f"Incomplete teacher period coverage: {teacher}")
        for slot, lessons in slots.items():
            if len({lesson["room"] for lesson in lessons}) > 1:
                logging.getLogger(__name__).warning("Teacher timetable itself lists multiple rooms: %s, weekday %s, period %s",
                                                    teacher, *slot)
            for lesson in lessons:
                key = (lesson["class"], *slot)
                if key not in assignments or not lesson["teacher"] or lesson["teacher"]["code"] != teacher:
                    raise ValueError(f"Unexpected teacher assignment: {teacher}, {key}")
                if lesson["changed"] or any(
                    old["group"] == lesson["group"] and old["teacher"] and old["teacher"]["code"] == teacher
                    for old in current_slots[key]["lessons"]
                ):
                    assignments[key].append(lesson)
    for key, period in current_slots.items():
        old_lessons = period["lessons"]
        authoritative = []
        for group in dict.fromkeys(lesson["group"] for lesson in assignments[key]):
            candidates = [lesson for lesson in assignments[key] if lesson["group"] == group]
            changed = [lesson for lesson in candidates if lesson["changed"]]
            candidates = changed or candidates
            if len(candidates) != 1:
                raise ValueError(f"Conflicting teachers for class group: {key}, {group}")
            authoritative.append(candidates[0])
        new_groups = {lesson["group"] for lesson in authoritative}
        retained = [lesson for lesson in old_lessons
                    if (not lesson["teacher"] or lesson["teacher"]["code"] not in teachers)
                    and lesson["group"] not in new_groups]
        for source in authoritative:
            matches = [lesson for lesson in old_lessons
                       if lesson["group"] == source["group"] and lesson_values(lesson) == lesson_values(source)]
            if matches:
                retained.append(matches[0])
            else:
                lesson = deepcopy(source)
                lesson.pop("class")
                lesson["changed"] = source["changed"] or not any(
                    old["group"] == lesson["group"] and lesson_values(old) == lesson_values(lesson)
                    for old in regular_slots.get(key, ())
                )
                retained.append(lesson)
        signature = lambda lessons: sorted((repr(lesson["group"]), repr(lesson_values(lesson))) for lesson in lessons)
        if signature(old_lessons) != signature(retained):
            logging.getLogger(__name__).info("Teacher timetable corrects class %s, weekday %s, period %s: %s -> %s",
                                             *key, signature(old_lessons), signature(retained))
        period["lessons"] = retained
    return result


def reconcile_rooms(current, regular_rooms, current_rooms, authoritative_teachers=()):
    if not regular_rooms or regular_rooms.keys() != current_rooms.keys():
        raise ValueError("Regular and substitution room lists differ")
    for room in regular_rooms:
        if regular_rooms[room].keys() != current_rooms[room].keys():
            raise ValueError(f"Room period headers differ: {room}")
    result = deepcopy(current)
    for table in result["timetables"]:
        for day in table["days"]:
            for period in day["periods"]:
                slot = WEEKDAYS[day["day"]], period["number"]
                for lesson in list(period["lessons"]):
                    if lesson["teacher"] and lesson["teacher"]["code"] in authoritative_teachers:
                        continue
                    room = lesson["room"]
                    if room is None:
                        continue
                    if room not in current_rooms or slot not in current_rooms[room]:
                        raise ValueError(f"Missing room coverage: {room}, {slot}")
                    key = (table["class"], lesson["group"], *lesson_values(lesson)[:2])
                    if key in current_rooms[room][slot]:
                        continue
                    if lesson["changed"] or key not in regular_rooms[room][slot]:
                        raise ValueError(f"Conflicting class and room lesson: {key}, {slot}")
                    if any(entry[:2] == key[:2] for slots in current_rooms.values() for entry in slots.get(slot, ())):
                        raise ValueError(f"Unresolved room reassignment: {key}, {slot}")
                    period["lessons"].remove(lesson)
                    logging.getLogger(__name__).info("Room timetable confirms cancellation: %s, weekday %s, period %s, group %s, room %s",
                                                     table["class"], *slot, lesson["group"], room)
    return result


def checked_text(value, limit, field, required=False):
    if value is None and not required:
        return value
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"Invalid {field}: {value!r}")
    return value


def teacher_name_key(name):
    name = re.sub(r"\b(?:Mgr|MgA|Ing|Bc|PhDr|RNDr|PaedDr|JUDr|MUDr|PhD|DiS|CSc)\.", "", name, flags=re.I)
    return tuple(sorted(re.findall(r"[^\W\d_]+", name.casefold())))


def lesson_values(lesson):
    if lesson is None:
        return (None, None, None)
    return (lesson["subject"], lesson["teacher"]["code"] if lesson["teacher"] else None, lesson["room"])


def build_substitutions(code, weekday, period, old_lessons, new_lessons):
    remaining = list(old_lessons)
    old_lunches = [lesson for lesson in old_lessons if lesson["subject"] == "oběd"]
    if (len(new_lessons) == 1 and new_lessons[0]["subject"] == "oběd" and new_lessons[0]["group"] is None
            and not new_lessons[0]["changed"] and len(old_lunches) == 1):
        new_lessons = [dict(new_lessons[0], group=old_lunches[0]["group"])]
    current_groups = {lesson["group"] for lesson in new_lessons}
    if (len(remaining) == 1 and remaining[0]["subject"] == "oběd" and remaining[0]["group"] is None
            and current_groups and current_groups <= GROUP_PARTNERS.keys()):
        groups = current_groups | {GROUP_PARTNERS[group] for group in current_groups}
        remaining = [dict(remaining[0], group=group) for group in sorted(groups)]
    for lesson in new_lessons:
        if lesson["changed"]:
            continue
        matches = [old for old in remaining if old["group"] == lesson["group"] and lesson_values(old) == lesson_values(lesson)]
        if not matches:
            raise ValueError(f"Unmarked timetable change in {code}, weekday {weekday}, period {period}")
        remaining.remove(matches[0])
    substitutions = []
    for lesson in new_lessons:
        if not lesson["changed"]:
            continue
        candidates = [old for old in remaining if old["group"] == lesson["group"]]
        if len(candidates) > 1 and any(lesson_values(old) != lesson_values(candidates[0]) for old in candidates):
            raise ValueError(f"Ambiguous changed lessons in {code}, weekday {weekday}, period {period}")
        old = candidates[0] if candidates else None
        if old is not None:
            remaining.remove(old)
        substitutions.append((
            code, weekday, period, lesson["group"], "OTHER",
            *lesson_values(old), *lesson_values(lesson), lesson["text"],
        ))
    for old in remaining:
        if old["group"] in current_groups:
            raise ValueError(f"Ambiguous changed lessons in {code}, weekday {weekday}, period {period}")
        substitutions.append((
            code, weekday, period, old["group"], "CANCELLED",
            *lesson_values(old), *lesson_values(None), None,
        ))
    return substitutions


def build_snapshot(regular, current):
    teachers, subjects, aliases = {}, {}, {}
    for page in (regular, current):
        metadata = list(page["teachers"])
        for table in page["timetables"]:
            for day in table["days"]:
                for period in day["periods"]:
                    for lesson in period["lessons"]:
                        if lesson["teacher"]:
                            metadata.append(lesson["teacher"])
                        abbrev = checked_text(lesson["subject"], 10, "subject abbreviation", True)
                        name = checked_text(lesson["subject_name"], 100, "subject name")
                        subjects[abbrev] = name or subjects.get(abbrev)
                        checked_text(lesson["room"], 20, "room")
        for teacher in metadata:
            abbrev = checked_text(teacher["code"], 2, "teacher abbreviation", True)
            name = checked_text(teacher["name"], 100, "teacher name")
            teachers[abbrev] = name or teachers.get(abbrev)
            if name:
                aliases.setdefault(teacher_name_key(name), set()).add(abbrev)

    classes, timetable, substitutions = [], [], []
    regular_tables = {table["class"]: table for table in regular["timetables"]}
    current_tables = {table["class"]: table for table in current["timetables"]}
    if not regular_tables or regular_tables.keys() != current_tables.keys():
        raise ValueError("Regular and substitution class lists differ")
    for code, table in regular_tables.items():
        checked_text(code, 10, "class code", True)
        if not any(period["lessons"] for day in table["days"] for period in day["periods"]):
            raise ValueError(f"No regular lessons found for {code}")
        current_table = current_tables[code]
        if (table["class_teacher"], table["homeroom"]) != (current_table["class_teacher"], current_table["homeroom"]):
            raise ValueError(f"Class metadata differs between pages for {code}")
        name = table["class_teacher"]
        matches = {name} if name in teachers else aliases.get(teacher_name_key(name), set())
        if len(matches) != 1:
            raise ValueError(f"Cannot resolve class teacher abbreviation for {code}: {name}")
        classes.append((code, next(iter(matches)), checked_text(table["homeroom"], 20, "home classroom", True)))
        current_days = {day["day"]: day for day in current_table["days"]}
        for day in table["days"]:
            weekday = WEEKDAYS[day["day"]]
            new_periods = {period["number"]: period for period in current_days[day["day"]]["periods"]}
            if {period["number"] for period in day["periods"]} != new_periods.keys():
                raise ValueError(f"Period headers differ between pages for {code}")
            for period in day["periods"]:
                number = period["number"]
                old_lessons = period["lessons"]
                new_lessons = new_periods[number]["lessons"]
                for lesson in old_lessons:
                    if lesson["changed"]:
                        raise ValueError("Regular timetable unexpectedly contains a changed lesson")
                    timetable.append((code, weekday, number, *lesson_values(lesson), lesson["group"]))
                substitutions.extend(build_substitutions(code, weekday, number, old_lessons, new_lessons))
    if not timetable:
        raise ValueError("No regular lessons found")
    return {
        "teachers": list(teachers.items()),
        "subjects": list(subjects.items()),
        "classes": classes,
        "timetable": timetable,
        "substitutions": substitutions,
    }


class TimetableSpider(scrapy.Spider):
    name = "sssvt_timetables"
    allowed_domains = ["www.sssvt.cz"]
    start_urls = [REGULAR_URL, SOURCE_URL]
    custom_settings = {
        "ROBOTSTXT_OBEY": False,
        "REDIRECT_ENABLED": False,
        "METAREFRESH_ENABLED": False,
        "COOKIES_ENABLED": False,
        "HTTPCACHE_ENABLED": False,
        "DOWNLOAD_TIMEOUT": 30,
        "RETRY_TIMES": 2,
        "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "LOG_LEVEL": "INFO",
        "TELNETCONSOLE_ENABLED": False,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pages = {}
        self.rooms = {REGULAR_URL: {}, SOURCE_URL: {}}
        self.expected_rooms = {}
        self.teachers = {}

    def parse(self, response):
        self.pages[response.url] = parse_page(response)
        self.logger.info("Parsed %d class timetables from %s", len(self.pages[response.url]["timetables"]), response.url)
        if response.url == SOURCE_URL:
            links = response.css('p.links a[href*="/teacher/"]')
            names = [text(link) for link in links]
            if not names or any(not name for name in names) or len(set(names)) != len(names):
                raise ValueError("Missing or duplicate advertised teachers")
            for teacher in self.pages[response.url]["teachers"]:
                url = REGULAR_URL + f"teacher/{quote(teacher['code'], safe='')}/suplovaci/"
                yield scrapy.Request(url, callback=self.parse_teacher, cb_kwargs={"teacher": teacher})
        links = response.css('p.links a[href*="/room/"]')
        names = [text(link) for link in links]
        if not names or len(set(names)) != len(names):
            raise ValueError("Missing or duplicate advertised rooms")
        self.expected_rooms[response.url] = set(names)
        for link, room in zip(links, names):
            url = response.urljoin(link.attrib["href"]).split("#")[0].rstrip("/") + "/"
            yield scrapy.Request(url, callback=self.parse_room, cb_kwargs={"source": response.url, "room": room})

    def parse_room(self, response, source, room):
        self.rooms[source][room] = parse_room_page(response, room)

    def parse_teacher(self, response, teacher):
        self.teachers[teacher["code"]] = parse_teacher_page(response, teacher)


def main():
    parser = argparse.ArgumentParser(description="Replace the SSŠVT PostgreSQL timetable snapshot after a successful scrape.")
    parser.parse_args()
    with logged_run("timetables"):
        run_scrape()


def run_scrape():
    database_url = os.environ.get("DATABASE_URL")
    try:
        scrape()
    except Exception:
        if database_url:
            try:
                save_scrape_status(database_url, "timetables", False)
            except Exception:
                logging.getLogger(__name__).exception("Failed to record timetables scrape status")
        raise
    save_scrape_status(database_url, "timetables", True)


def scrape():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL connection string.")
    process = CrawlerProcess(install_root_handler=False)
    crawler = process.create_crawler(TimetableSpider)
    process.crawl(crawler)
    process.start()
    pages = crawler.spider.pages if crawler.spider else {}
    stats = crawler.stats.get_stats()
    if set(pages) != {REGULAR_URL, SOURCE_URL} or stats.get("finish_reason") != "finished" or stats.get("log_count/ERROR", 0):
        raise RuntimeError("Scrape failed; the database was not changed.")
    try:
        for source in (REGULAR_URL, SOURCE_URL):
            if crawler.spider.rooms[source].keys() != crawler.spider.expected_rooms.get(source):
                raise ValueError(f"Incomplete room scrape: {source}")
        current = reconcile_teachers(pages[REGULAR_URL], pages[SOURCE_URL], crawler.spider.teachers)
        current = reconcile_rooms(current, crawler.spider.rooms[REGULAR_URL], crawler.spider.rooms[SOURCE_URL],
                                  authoritative_teachers=crawler.spider.teachers)
        snapshot = build_snapshot(pages[REGULAR_URL], current)
    except ValueError as error:
        raise RuntimeError(f"Validation failed; the database was not changed: {error}") from error
    try:
        logging.getLogger(__name__).info("Validation passed; saving snapshot to PostgreSQL")
        save_snapshot(snapshot, database_url)
    except Exception as error:
        raise RuntimeError(f"Database save failed ({type(error).__name__}); the transaction was not committed.") from None
    logging.getLogger(__name__).info(f"Saved {len(snapshot['classes'])} classes, {len(snapshot['timetable'])} regular lessons and {len(snapshot['substitutions'])} substitutions to PostgreSQL.")


if __name__ == "__main__":
    main()
