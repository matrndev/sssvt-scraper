import argparse
import logging
import os
import re
from urllib.parse import unquote

import scrapy
from dotenv import load_dotenv
from scrapy.crawler import CrawlerProcess

from scrapers.logging_setup import logged_run

from database import save_contacts, save_scrape_status


load_dotenv()

SOURCE_URL = "https://www.sssvt.cz/kontakty/ucitele/"


def clean(value):
    return " ".join(value.split())


def parse_consultation(value):
    hours, separator, room = clean(value).partition("|")
    if separator:
        match = re.fullmatch(r"místnost\s*:?\s*(.*)", room.strip(), re.I)
        if not match:
            raise ValueError(f"Unrecognized consultation room: {room!r}")
        room = match[1].strip()
        if len(room) > 20:
            raise ValueError(f"Consultation room is too long: {room!r}")
    return {"hours": hours.strip() or None, "room": room or None}


def parse_contact(card):
    details = card.xpath("./span")
    name = clean(details.css(".bluetext").xpath("string(.)").get(default=""))
    metadata = clean(details.xpath("string(.)").get(default=""))
    match = re.search(r"Zkratka:\s*([^|]+)\|\s*Kabinet:\s*([^|]*)\|\s*Tel\.\s*číslo:", metadata)
    if not match or not name or len(name) > 100:
        raise ValueError("Missing or invalid teacher metadata")
    abbrev, room = (value.strip() for value in match.groups())
    links = card.xpath('./a[contains(@href, "/teacher/")]/@href').getall()
    if (not 0 < len(abbrev) <= 2 or abbrev != card.attrib.get("id")
            or len(links) != 1 or f"/teacher/{abbrev}/" not in unquote(links[0]) or len(room) > 20):
        raise ValueError(f"Invalid teacher abbreviation, link or room: {abbrev!r}")
    emails = details.css('a[href^="mailto:"]::attr(href)').getall()
    phones = details.css('a[href^="tel:"]::attr(href)').getall()
    if len(emails) > 1 or len(phones) > 1:
        raise ValueError(f"Multiple contact links for {abbrev}")
    email = unquote(emails[0][7:]).split("?", 1)[0].strip() if emails else None
    phone = re.sub(r"\s+", "", phones[0][4:]) if phones else None
    if email is not None and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError(f"Invalid email for {abbrev}")
    if phone is not None and not re.fullmatch(r"\+?\d+", phone):
        raise ValueError(f"Invalid phone number for {abbrev}")
    consultations = [clean(value) for value in details.xpath("./text()").getall() if "Konzultace:" in value]
    if len(consultations) != 1:
        raise ValueError(f"Missing or ambiguous consultation field for {abbrev}")
    return {
        "abbrev": abbrev,
        "name": name,
        "email": email,
        "room": room or None,
        "phone_number": phone,
        "consultation": parse_consultation(consultations[0].split("Konzultace:", 1)[1]),
    }


def parse_page(response):
    if response.url != SOURCE_URL or response.status != 200:
        raise ValueError(f"Unexpected contacts response: {response.status} {response.url}")
    contacts = [parse_contact(card) for card in response.css("ul.kontakty > li")]
    if not contacts or len({contact["abbrev"] for contact in contacts}) != len(contacts):
        raise ValueError("Missing or duplicate teacher contacts")
    return contacts


class ContactsSpider(scrapy.Spider):
    name = "sssvt_contacts"
    allowed_domains = ["www.sssvt.cz"]
    start_urls = [SOURCE_URL]
    custom_settings = {
        "ROBOTSTXT_OBEY": False,
        "REDIRECT_ENABLED": False,
        "METAREFRESH_ENABLED": False,
        "COOKIES_ENABLED": False,
        "DOWNLOAD_TIMEOUT": 30,
        "RETRY_TIMES": 2,
        "USER_AGENT": "Mozilla/5.0",
        "LOG_LEVEL": "INFO",
        "TELNETCONSOLE_ENABLED": False,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.contacts = None

    def parse(self, response):
        self.contacts = parse_page(response)
        self.logger.info("Parsed %d teacher contacts from %s", len(self.contacts), response.url)


def main():
    parser = argparse.ArgumentParser(description="Update SSŠVT teacher contacts and consultations in PostgreSQL.")
    parser.parse_args()
    with logged_run("contacts"):
        run_scrape()


def run_scrape():
    database_url = os.environ.get("DATABASE_URL")
    try:
        scrape()
    except Exception:
        if database_url:
            try:
                save_scrape_status(database_url, "contacts", False)
            except Exception:
                logging.getLogger(__name__).exception("Failed to record contacts scrape status")
        raise
    save_scrape_status(database_url, "contacts", True)


def scrape():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL connection string.")
    process = CrawlerProcess(install_root_handler=False)
    crawler = process.create_crawler(ContactsSpider)
    process.crawl(crawler)
    process.start()
    contacts = crawler.spider.contacts if crawler.spider else None
    stats = crawler.stats.get_stats()
    if not contacts or stats.get("finish_reason") != "finished" or stats.get("log_count/ERROR", 0):
        raise RuntimeError("Contacts scrape failed; the database was not changed.")
    try:
        logging.getLogger(__name__).info("Saving %d teacher contacts to PostgreSQL", len(contacts))
        save_contacts(contacts, database_url)
    except Exception as error:
        raise RuntimeError(f"Database save failed ({type(error).__name__}); the transaction was not committed.") from None
    logging.getLogger(__name__).info(f"Saved contacts and consultations for {len(contacts)} teachers to PostgreSQL.")


if __name__ == "__main__":
    main()
