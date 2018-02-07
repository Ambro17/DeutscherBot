import requests
import re
from datetime import datetime as d
import sqlite3
import os
import json
import time

from lxml.html import fragments_fromstring, fragment_fromstring, HtmlElement
from lxml import etree
import praw

import config
from exceptions import (
    DeutscherBotException,
    CouldNotGetText,
    SearchError,
    TranslationException,
)
DB_PATH = os.path.join(os.path.dirname(__file__), 'db')

STATUS_TO_REASON = {
    200: "Request successful",
    204: "No results could be found for the given word",
    404: "The dictionary does not exist",
    403: "Supplied credentials could not be verified, or access to dictionary denied",
    500: "A server error has occurred", None: "Unknown error (sorry)"
}

class Pons(object):
    API_BASE_URL = 'https://api.pons.com/v1/dictionary'
    LANGUAGE = 'l'
    SEARCH_STRING = 'q'
    INPUT_LANG = 'in'
    SEARCH_URL = "https://en.pons.com/translate?q={word}&l=deen&in=de&language=en"
    auth = {'X-Secret': config.PONS_KEY}

    def search(self, word):
        """Receives a word and returns the result of pons dictionary."""
        params = ((Pons.LANGUAGE, 'deen'), (Pons.SEARCH_STRING, word),
                  (Pons.INPUT_LANG, 'de'), ('language', 'en'))
        response = requests.get(Pons.API_BASE_URL, headers=Pons.auth,
                                params=params)
        status = response.status_code
        if status == 200:
            return response.json()[0]
        else:
            error_msg = STATUS_TO_REASON.get(status)
            raise SearchError(error_msg)

class DeutscherBot():
    """Given a word it returns it's gender, translation, and a usage example."""
    dbot = praw.Reddit("DeutscherBot")
    BREAK_LINE = '\n\n'
    BLANK_LINE = BREAK_LINE + '&nbsp;' + BREAK_LINE
    COMMENT_TEMPLATE = (
        "{article_and_word} {phonetics} | {word_type}" + BLANK_LINE +
        "🇩🇪 {word} 🔁 🇬🇧 {translation}" + BLANK_LINE +
        "{source_link}" # + {example}
        )

    db_connection = sqlite3.connect(os.path.join(DB_PATH, 'posts2.db'),
                                    detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)

    def __init__(self, subreddit='DeutschesBot', create_db = False):
        self.pons = Pons()
        self.subreddit = self.dbot.subreddit(subreddit)
        self.db_cursor = self.db_connection.cursor()
        if create_db:
            self.db_cursor.execute('''CREATE TABLE posts(
                post_id, link, word, translation, raw_result, date, subreddit)''')

    def scan_posts(self, cant=5):
        print(f"Visting /r/{self.subreddit}")
        for post in self.subreddit.new(limit=cant):
            print(f"Visiting {post.title}")
            if not self.visited_db(post):
                word = self._get_word_to_search(post)
                word_details = self.search_word(word)
                definition = self.prepare_comment(word_details)
                post.reply(definition)
                print(f"Replied on https://reddit.com/{post.id}")
                self.add_to_db(post, word_details)
                delay = 20
            else:
                print(f"Skipping '{post.title}', already visited")
                delay = 5

            print(f"Sleeping for {delay} seconds...")
            time.sleep(delay)

        print("My job has ended")

    def _get_word_to_search(self, post):
        """Extract last word from post title.

        Format is
            Wort of the hour: <word>
        """
        # Split Words
        words = post.title.split()
        # Get last word without spaces
        word = words[-1].strip()
        return word

    def search_word(self, s_word):
        search_result = dict()
        try:
            result = self.pons.search(s_word)
        except SearchError:
            raise DeutscherBotException(f"Error searching {s_word}")

        # Get Most relevant result.
        result = result['hits'][0]
        if result['type'] == 'entry':
            definition = result['roms'][0]
        else:
            raise TranslationException("Translations are not yet supported")
        # Get result word
        word = definition['headword'].replace('·','') # remove syllable separator if present
        search_result['word'] = word

        # Get word class, noun|verb|adverb|adj
        wordclass = definition.get('wordclass')
        if wordclass:
            search_result['word_type'] = wordclass

        # Get plural, phonetics and other metadata.
        word_metadata = self.get_word_metadata(definition['headword_full'])
        search_result['metadata'] = word_metadata

        gender = word_metadata['genus'] if wordclass == 'noun' else None # replace with get
        if gender:
            search_result['gender'] = gender

        translation = definition['arabs'][0]['translations'][0]
        if translation:
            search_result['translation'] = self.get_text_from_irregular_string(
                translation['target'])

        example = self.get_example(definition)
        if self.get_example(definition):
            search_result['example'] = example

        return search_result

    LETTER_TO_ARTICLE_MAPPER = {
        'nt': 'das',
        'm': 'der',
        'f': 'die'
    }
    def prepare_comment(self, word_result):
        try:
            article = self.LETTER_TO_ARTICLE_MAPPER[word_result.get('gender', '')]
        except KeyError:
            # Word has no article (is verb, adverb, adjective or similar)
            article=''
        word = word_result.get('word')
        word_w_article = article+' '+word if article else word
        word_type = word_result.get('word_type').title()
        phonetics = str(word_result['metadata'].get('phonetics', ''))
        translation = word_result['translation']
        source = self.script(self.format_link(
            "PONS-reference", self.pons.SEARCH_URL.format(word=word)))
        # Add example if there is one, if not put second translation?
        return self.COMMENT_TEMPLATE.format(
            article_and_word=self.bold(word_w_article),
            phonetics=phonetics,
            word_type=self.italics(word_type) or None,
            word = word,
            translation=translation,
            source_link=source,
        )

    def get_example(self, adefinition):
        # filter translation which has phrases as header
        phrase = list(filter(
            lambda entry: entry['header'] == "Phrases:",
            adefinition['arabs']))
        if phrase:
            translations = phrase[0]['translations']
            translation = translations[0]
            source = self.get_text_from_irregular_string(translation['source'])
            target = self.get_target_text(translation['target'])
            return (source, target)
        else:
            # no translation example was found
            return None

    # DEPRECATED
    def get_source_text(self, astr):
        """Gets text from html string."""
        spans = fragments_fromstring(astr)
        source = spans[0].text_content()
        return source

    def get_target_text(self, astr):
        """Removes html tags from string that represents translation

        Consider improving intelligence and parsing rest of string in italics..
        """
        html_tags = '<[^>]*>'
        return re.sub(html_tags, '', astr)

    def get_text_from_irregular_string(self, astr):
        """"Tries to correctly format a string with embedded html.

            input: 'text outside <span class="info">text<acronym title="plural">inside</acronym></span>'
            output: 'text outside (*text inside*)'

        As seen in the example, inner text is wrapped in asteriscs
        """
        string_pieces = fragments_fromstring(astr)
        text = ""
        for elem in string_pieces:
            if isinstance(elem, str):
                text += elem
            elif isinstance(elem, HtmlElement):
                inner_text = elem.text_content()
                text += self.parenthesis(self.italics(inner_text))
            else:
                raise CouldNotGetText(f"No method to extract text from {elem}"
                                f" of type {type(elem)} to string")

        return text

    def get_word_metadata(self, fullword):
        """Get dictionary of attributes associated with a word.

        Build dictionary of word details from raw string with invalid html.
        """
        # Build list of html elements, ignoring leading string.
        html_elem = fragments_fromstring(fullword)[1:]
        metadata = {}
        for elem in html_elem:
            key = elem.get('class')
            if key not in metadata:
                metadata[key] = elem.text_content()

        return metadata

    def add_to_db(self, post, word_details):
        word = word_details['word']
        translation = word_details['translation']
        raw_result = json.dumps(word_details)
        now = d.now()
        self.db_cursor.execute("""INSERT INTO posts(
            post_id, link, word, translation, raw_result, date, subreddit)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (post.id, post.url, word, translation, raw_result, now, str(self.subreddit)))

        self.db_connection.commit()

    def visited_db(self, post):
        self.db_cursor.execute("""SELECT * FROM posts WHERE EXISTS 
                               (SELECT 1 FROM posts 
                               WHERE post_id = (?))""", (post.id,))
        return bool(self.db_cursor.fetchone())

    def format_link(self, visible_name, url):
        return f"[{visible_name}]({url})"

    def bold(self, astr):
        return f"**{astr}**"

    def script(self, astr):
        return f"^{astr}"

    def italics(self, astr):
        return f"*{astr}*"

    def parenthesis(self, astr):
        return f"({astr})"

DeutscherBot("Sprache").scan_posts(cant=20)

"""
dbot = DeutscherBot()


words3 = sample_200 = ['Vermeidet', 'Western', 'Ordner', 'Konzil', 'Vorsichtig', 'Hört', 'Gewaltsam', 'Dampf', 'Sklave', 'Preis', 'Bibel', 'Review', 'Ein', 'Durcheinander', 'Bejahend', 'Ornament', 'Zügig', 'Gelb', 'Gefängnis', 'Orange', 'Zutiefst', 'Bronze', 'Hoffnung', 'Firma', 'Andacht', 'Reif', 'Verwandt', 'Zerstört', 'Speer', 'Nacht', 'Flamme', 'Kombination', 'Unterbrechung', 'Betont', 'Schlimm', 'Sicherheit', 'Vorsilbe', 'Reich', 'Sicher', 'Rückenseitig', 'Journal', 'Caritas', 'Geschenk', 'Verbringt', 'Vater', 'Symmetrisch', 'Unmut', 'Lösbar', 'Zügelt', 'Pfiffig', 'Besitzt', 'Stift', 'Verbaut', 'Literarisch', 'Theorie', 'Abhilfe', 'Methode', 'Geschmack', 'Bündel', 'Schlacht', 'Nase', 'More', 'Sprungweite', 'Hube', 'Wachst', 'Treu', 'Ente', 'Sorglos', 'Darm', 'überredung/überredung', 'Periodisch', 'Geständnis', 'Nummer', 'Realität', 'Närrisch', 'An', 'Verdauung', 'Tiefe', 'Betrüger', 'Vertrag', 'Spekulation', 'Komparativ', 'Vollendeten', 'Tausend', 'Symmetrie', 'Kräftig', 'Angenehm', 'überwindet', 'Schlich', 'Appetit', 'Orchester', 'Rede', 'Debatte', 'Begeht', 'Real', 'Schwerkraft', 'Ungelenk', 'Duft', 'Irrtümlich', 'Straßengraben', 'Hilfsverb', 'Droben', 'Leicht', 'Geschickt', 'Abfolge', 'ähnelt', 'Kraftvoll', 'Wohlüberlegt', 'Geradheit', 'Bart', 'Wärme', 'Still', 'Region', 'Belegt', 'Mitte', 'Rille', 'Wappen', 'Flasche', 'Stattet', 'Kader', 'Name', 'Staub', 'Leiter', 'Gelegenheit', 'Seicht', 'Moralisch', 'Feindlich', 'Trügt', 'Koppelt', 'Geschicklichkeit', 'Zivil', 'Metallisch', 'Chemikalie', 'Klasse', 'Moment', 'Expandiert', 'Motor', 'Besiegung', 'Burg', 'Gehört', 'Leere', 'Baum', 'Versetzung', 'Fond', 'Scheibe', 'Blattwerk', 'Erfolg', 'Bund', 'Zimmer', 'Folter', 'Portion', 'Vorgesehen', 'Verstoß', 'Streng', 'Bleistift', 'Betrügt', 'Weiterhin', 'An', 'Wahr', 'Tool', 'Gold', 'Datum', 'Barsch', 'Multiplikation', 'Wiege', 'Zement', 'Misserfolg', 'Wunde', 'Vers', 'Fäulnis', 'Kabel', 'Verbal', 'Inhalt', 'Vertikal', 'Tropisch', 'Kandidatin', 'Kriminelle', 'Feierlich', 'Ausmaß', 'Energie', 'Schaden', 'Umher', 'Urkunde', 'Dach', 'Mahlzeit', 'Männchen', 'Schenkel', 'Entgelt', 'Schüssel', 'Auswertung', 'Defekt', 'Angemessen', 'Krankheit', 'Heck', 'Olive', 'Norden', 'Stuhl', 'Offen', 'Prozession', 'Kohlkopf']
sample_500 = ['öffentlich', 'Vermeidet', 'Western', 'Ordner', 'Konzil', 'Vorsichtig', 'Hört', 'Gewaltsam', 'Dampf', 'Sklave', 'Preis', 'Bibel', 'Review', 'Ein', 'Durcheinander', 'Bejahend', 'Ornament', 'Zügig', 'Gelb', 'Gefängnis', 'Orange', 'Zutiefst', 'Bronze', 'Hoffnung', 'Firma', 'Andacht', 'Reif', 'Verwandt', 'Zerstört', 'Speer', 'Nacht', 'Flamme', 'Kombination', 'Unterbrechung', 'Betont', 'Schlimm', 'Sicherheit', 'Vorsilbe', 'Reich', 'Sicher', 'Rückenseitig', 'Journal', 'Caritas', 'Geschenk', 'Verbringt', 'Vater', 'Symmetrisch', 'Unmut', 'Lösbar', 'Zügelt', 'Pfiffig', 'Besitzt', 'Stift', 'Verbaut', 'Literarisch', 'Theorie', 'Abhilfe', 'Methode', 'Geschmack', 'Bündel', 'Schlacht', 'Nase', 'More', 'Sprungweite', 'Hube', 'Wachst', 'Treu', 'Ente', 'Sorglos', 'Darm', 'überredung/überredung', 'Periodisch', 'Geständnis', 'Nummer', 'Realität', 'Närrisch', 'An', 'Verdauung', 'Tiefe', 'Betrüger', 'Vertrag', 'Spekulation', 'Komparativ', 'Vollendeten', 'Tausend', 'Symmetrie', 'Kräftig', 'Angenehm', 'überwindet', 'Schlich', 'Appetit', 'Orchester', 'Rede', 'Debatte', 'Begeht', 'Real', 'Schwerkraft', 'Ungelenk', 'Duft', 'Irrtümlich', 'Straßengraben', 'Hilfsverb', 'Droben', 'Leicht', 'Geschickt', 'Abfolge', 'ähnelt', 'Kraftvoll', 'Wohlüberlegt', 'Geradheit', 'Bart', 'Wärme', 'Still', 'Region', 'Belegt', 'Mitte', 'Rille', 'Wappen', 'Flasche', 'Stattet', 'Kader', 'Name', 'Staub', 'Leiter', 'Gelegenheit', 'Seicht', 'Moralisch', 'Feindlich', 'Trügt', 'Koppelt', 'Geschicklichkeit', 'Zivil', 'Metallisch', 'Chemikalie', 'Klasse', 'Moment', 'Expandiert', 'Motor', 'Besiegung', 'Burg', 'Gehört', 'Leere', 'Baum', 'Versetzung', 'Fond', 'Scheibe', 'Blattwerk', 'Erfolg', 'Bund', 'Zimmer', 'Folter', 'Portion', 'Vorgesehen', 'Verstoß', 'Streng', 'Bleistift', 'Betrügt', 'Weiterhin', 'An', 'Wahr', 'Tool', 'Gold', 'Datum', 'Barsch', 'Multiplikation', 'Wiege', 'Zement', 'Misserfolg', 'Wunde', 'Vers', 'Fäulnis', 'Kabel', 'Verbal', 'Inhalt', 'Vertikal', 'Tropisch', 'Kandidatin', 'Kriminelle', 'Feierlich', 'Ausmaß', 'Energie', 'Schaden', 'Umher', 'Urkunde', 'Dach', 'Mahlzeit', 'Männchen', 'Schenkel', 'Entgelt', 'Schüssel', 'Auswertung', 'Defekt', 'Angemessen', 'Krankheit', 'Heck', 'Olive', 'Norden', 'Stuhl', 'Offen', 'Prozession', 'Kohlkopf', 'Freundlich', 'Loch', 'Begeisterung', 'Opponiert', 'Gefüge', 'Genug', 'Begierde', 'Verteidigt', 'Betrag', 'Schulter', 'Jahr', 'Wartezeit', 'Sand', 'Ritt', 'Mond', 'Hauptsächlich', 'Schicht', 'Segeltuch', 'Fähig', 'Gegenwert', 'Scham', 'Rezeption', 'Bohle', 'Winkel', 'umwerfend', 'Involviert', 'Petition', 'Muskulös', 'Medizin', 'Unwahrheit', 'Ständig', 'Geheimnis', 'Auswuchs', 'An', 'Erzählt', 'Weh', 'Adrett', 'Looping', 'Transport', 'Günstig', 'Freiheit', 'Day!', 'Anspruch', 'Erlaubt', 'Folgend', 'Irdisch', 'Himmlisch', 'Marinesoldat', 'Aufregung', 'Kantig', 'Einbuße', 'Narr', 'Glück', 'Drüse', 'Truhe', 'Militär', 'Bad', 'Messing', 'Belohnung', 'Glaube', 'Kasse', 'Zuverlässig', 'Handfläche', 'Spät', 'Cord', 'Andernfalls', 'Allgemein', 'Extensiv', 'Mangelhaft', 'Aufrecht', 'Diener', 'Bitte', 'Jagd', 'Mittelbar', 'Königreich', 'Fremd', 'Würde', 'Aufsicht', 'Inversion', 'Geruch', 'Nobel', 'Nackt', 'Großartig', 'Vision', 'Verkehr', 'Spezies', 'Belegten', 'Hin', 'Abwärts', 'Konfusion', 'Gewöhnlich', 'Papiertaschentuch', 'Polin', 'Weibchen', 'Ab', 'An', 'Rose', 'Krabbe', 'Konus', 'Erkältung', 'Kühn', 'Furunkel', 'Unterschieden', 'Gedanke', 'Auf', 'Entzückung', 'Gewiss', 'Denkt', 'Grat', 'Praktisch', 'Ruhm', 'Tanz', 'Chef', 'Legierung', 'Instrumental', 'Zurück', 'Thron', 'Stigma', 'Geschmack', 'Himmel', 'Kies', 'Vorrichtung', 'Jährlich', 'Wand', 'Saat', 'Nelke', 'Milch', 'Dame', 'Angst', 'Ruhe', 'Kahl', 'Teilweise', 'Unbedeutend', 'Identisch', 'Haushalt', 'Genugtuung', 'Fund', 'Helm', 'Fördert', 'Sukzessiv', 'Gärt', 'Kompakt', 'Vereint', 'Paste', 'Nasal', 'Pflege', 'Umfassung', 'Innere', 'Belehrt', 'Abschätzung', 'Würdig', 'Stall', 'Streng', 'Profit', 'Zu', 'Yard', 'Röhre', 'Ruin', 'Gerade', 'Unterbrechung', 'Schein', 'Eigenwillig', 'Sammelt', 'Student', 'Spezial', 'Intensiv', 'Jahrhundert', 'Süden', 'Sauber', 'Klinge', 'Apparatur', 'Gemeinschaft', 'Wehrt', 'Kaufmann', 'Häufig', 'Doktrin', 'Konstante', 'Versammlung', 'Wasser', 'Unter', 'Andauernd', 'äußerung/äußerung', 'Fertig', 'Fossil', 'Baumwolle', 'Fungiert', 'Ein', 'Schuss', 'Bauernhof', 'Bulle', 'Deprimierten', 'Meister', 'Ein', 'Elegant', 'Steif', 'Musik', 'Geld', 'Fest', 'Uhr', 'Alarm', 'Aufschrift', 'Direkt', 'Definitiv', 'Cover', 'Tugend', 'Gesellschaftlich', 'Abfall', 'Priester', 'Niedrig', 'Wald', 'Ganz', 'Vergrößert', 'Holz', 'Klug', 'Langsam', 'Pein', 'Konventionell', 'Wichtig', 'Verschieden', 'Gewohnheitsmäßig', 'Kontinuierlich', 'Anlehnung', 'Seltsam', 'Zweck', 'Ein', 'Ehemann', 'Sprecht', 'Intention', 'Arbeitsgang', 'Richterlich', 'Gesinnt', 'Anhänger', 'Gift', 'Gefahr', 'übermittelt', 'Bresche', 'Schiff', 'Sorte', 'Kiel', 'Umfrage', 'Blume', 'Kummer', 'College', 'Unkraut', 'Königin', 'Abweichung', 'Sahne', 'Kind', 'Ritter', 'Matt', 'Post', 'Katholik', 'Erkenntnis', 'Binse', 'Stoß', 'Falte', 'Wahl', 'Leicht', 'Finsternis', 'Gewölbe', 'Ungewiss', 'Schwierig', 'Reisebus', 'Assistent', 'Angelegenheit', 'Vehikel', 'Rest', 'Vor', 'Anwalt', 'Wahrheit', 'Fährte', 'Farbe', 'Dur', 'Ziegel', 'Konstituiert', 'Gewalt', 'Reise', 'Spirale', 'Signal', 'Glocke', 'Heilig', 'Verharrt', 'Lob', 'Zusammenhang', 'Maut', 'Zimmer', 'Nett', 'Hals', 'Verfahren', 'Maske', 'Fleisch', 'Kur', 'Tätlich', 'Zerstörung', 'Ein', 'Bürger', 'Glaubt', 'Versuch', 'Anatomie']
print(f"words to search {len(sample_500)}")
for i, word in enumerate(sample_500):
    print(i)
    try:
        result = dbot.search_word(word)
    except NotImplementedError:
        print("Translation error")
        print(word)
        continue
    except DeutscherBotException:
        print(f"Exception for word {word}")
        continue
        
    definition = dbot.prepare_comment(result)
    print(definition)
    #  post.reply(definition)
"""