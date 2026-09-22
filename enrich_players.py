import json
import time
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# FYUCHA PLAYER DATABASE
# WIKIDATA ENRICHMENT V2
# ============================================================

INPUT_FILE = Path("output/players.json")

OUTPUT_FILE = Path(
    "output/enriched-players-test.json"
)

MATCH_FILE = Path(
    "output/wikidata-matches-test.json"
)

# ------------------------------------------------------------
# TEST MODE
# ------------------------------------------------------------
# IMPORTANT:
# We are still testing only 100 players.
#
# DO NOT increase this yet.
# We will only process the full database after the V2 test
# has been checked.
# ------------------------------------------------------------

TEST_LIMIT = 100


# ============================================================
# WIKIDATA
# ============================================================

WIKIDATA_API = (
    "https://www.wikidata.org/w/api.php"
)

USER_AGENT = (
    "FyuchaFootballBirthdays/2.0 "
    "(https://github.com/fyuchafc/fyucha-player-database)"
)


# ============================================================
# REQUEST SETTINGS
# ============================================================

REQUEST_TIMEOUT = 30

REQUEST_DELAY = 0.5

MAX_RETRIES = 4


# ============================================================
# CACHE
# ============================================================

ENTITY_CACHE = {}

SEARCH_CACHE = {}

LABEL_CACHE = {}


# ============================================================
# HTTP REQUEST
# ============================================================

def request_json(url, params=None):

    if params:

        url = (
            url
            + "?"
            + urlencode(params)
        )

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json"
                }
            )

            with urlopen(
                request,
                timeout=REQUEST_TIMEOUT
            ) as response:

                return json.loads(
                    response.read().decode(
                        "utf-8"
                    )
                )

        except HTTPError as error:

            last_error = error

            # ------------------------------------------------
            # HTTP 429 = rate limit
            # ------------------------------------------------

            if error.code == 429:

                wait_time = (
                    5 * attempt
                )

                print(
                    f"  Wikidata rate limit "
                    f"(429). Waiting "
                    f"{wait_time}s..."
                )

                time.sleep(
                    wait_time
                )

                continue

            # ------------------------------------------------
            # Temporary server errors
            # ------------------------------------------------

            if error.code in (
                500,
                502,
                503,
                504
            ):

                wait_time = (
                    3 * attempt
                )

                print(
                    f"  Wikidata server error "
                    f"{error.code}. Waiting "
                    f"{wait_time}s..."
                )

                time.sleep(
                    wait_time
                )

                continue

            raise

        except (
            URLError,
            TimeoutError
        ) as error:

            last_error = error

            wait_time = (
                3 * attempt
            )

            print(
                f"  Network error. "
                f"Retrying in "
                f"{wait_time}s..."
            )

            time.sleep(
                wait_time
            )

    raise last_error


# ============================================================
# SEARCH WIKIDATA
# ============================================================

def search_wikidata(name):

    cache_key = name.lower().strip()

    if cache_key in SEARCH_CACHE:

        return SEARCH_CACHE[
            cache_key
        ]

    params = {

        "action":
            "wbsearchentities",

        "search":
            name,

        "language":
            "en",

        "format":
            "json",

        "limit":
            10,

        "type":
            "item"
    }

    try:

        data = request_json(
            WIKIDATA_API,
            params
        )

        results = data.get(
            "search",
            []
        )

        SEARCH_CACHE[
            cache_key
        ] = results

        return results

    except Exception as error:

        print(
            f"Search error for "
            f"{name}: {error}"
        )

        SEARCH_CACHE[
            cache_key
        ] = []

        return []


# ============================================================
# GET WIKIDATA ENTITY
# ============================================================

def get_entity(qid):

    if qid in ENTITY_CACHE:

        return ENTITY_CACHE[
            qid
        ]

    params = {

        "action":
            "wbgetentities",

        "ids":
            qid,

        "format":
            "json",

        "languages":
            "en",

        "languagefallback":
            "1",

        "props":
            "labels|descriptions|aliases|claims"
    }

    try:

        data = request_json(
            WIKIDATA_API,
            params
        )

        entity = (
            data
            .get("entities", {})
            .get(qid)
        )

        ENTITY_CACHE[
            qid
        ] = entity

        return entity

    except Exception as error:

        print(
            f"Entity error for "
            f"{qid}: {error}"
        )

        ENTITY_CACHE[
            qid
        ] = None

        return None


# ============================================================
# GET CLAIMS
# ============================================================

def get_claims(entity):

    if not entity:

        return {}

    return entity.get(
        "claims",
        {}
    )


# ============================================================
# EXTRACT TIME CLAIM
# ============================================================

def get_time_claims(
    claims,
    property_id
):

    values = claims.get(
        property_id,
        []
    )

    results = []

    for statement in values:

        mainsnak = statement.get(
            "mainsnak",
            {}
        )

        datavalue = mainsnak.get(
            "datavalue"
        )

        if not datavalue:

            continue

        value = datavalue.get(
            "value"
        )

        if not isinstance(
            value,
            dict
        ):

            continue

        time_value = value.get(
            "time"
        )

        precision = value.get(
            "precision"
        )

        if not time_value:

            continue

        match = re.search(
            r"([+-]\d{4,})-(\d{2})-(\d{2})",
            time_value
        )

        if not match:

            continue

        year = (
            match
            .group(1)
            .lstrip("+")
        )

        month = (
            match
            .group(2)
        )

        day = (
            match
            .group(3)
        )

        results.append({

            "date":
                f"{year}-{month}-{day}",

            "precision":
                precision
        })

    return results


# ============================================================
# SELECT BEST DATE CLAIM
# ============================================================

def get_best_date_claim(
    claims,
    property_id
):

    values = get_time_claims(
        claims,
        property_id
    )

    if not values:

        return None

    # Prefer full day precision.
    #
    # Wikibase precision:
    #
    # 9  = year
    # 10 = month
    # 11 = day
    #
    # We therefore sort from highest precision
    # to lowest.

    values.sort(
        key=lambda item:
            item.get(
                "precision",
                0
            ),
        reverse=True
    )

    return values[0]


# ============================================================
# EXTRACT ENTITY CLAIM
# ============================================================

def get_claim_id(
    claims,
    property_id
):

    values = claims.get(
        property_id,
        []
    )

    if not values:

        return None

    # Prefer preferred-rank claims
    # when available.

    preferred = [
        item
        for item in values
        if item.get("rank")
        == "preferred"
    ]

    if preferred:

        values = preferred

    for statement in values:

        mainsnak = statement.get(
            "mainsnak",
            {}
        )

        datavalue = mainsnak.get(
            "datavalue"
        )

        if not datavalue:

            continue

        value = datavalue.get(
            "value"
        )

        if isinstance(
            value,
            dict
        ):

            qid = value.get(
                "id"
            )

            if qid:

                return qid

    return None


# ============================================================
# EXTRACT STRING CLAIM
# ============================================================

def get_claim_string(
    claims,
    property_id
):

    values = claims.get(
        property_id,
        []
    )

    if not values:

        return None

    for statement in values:

        mainsnak = statement.get(
            "mainsnak",
            {}
        )

        datavalue = mainsnak.get(
            "datavalue"
        )

        if not datavalue:

            continue

        value = datavalue.get(
            "value"
        )

        if isinstance(
            value,
            str
        ):

            return value

    return None


# ============================================================
# GET LABEL
# ============================================================

def get_entity_label(
    qid
):

    if not qid:

        return None

    if qid in LABEL_CACHE:

        return LABEL_CACHE[
            qid
        ]

    entity = get_entity(
        qid
    )

    if not entity:

        LABEL_CACHE[
            qid
        ] = None

        return None

    labels = entity.get(
        "labels",
        {}
    )

    label = None

    if "en" in labels:

        label = labels[
            "en"
        ].get(
            "value"
        )

    LABEL_CACHE[
        qid
    ] = label

    return label


# ============================================================
# NORMALIZE NAME
# ============================================================

def normalize_name(name):

    if not name:

        return ""

    name = name.lower()

    name = (
        name
        .replace("’", "'")
        .replace("ʻ", "'")
        .replace("`", "'")
    )

    # Remove apostrophes rather than
    # treating them as meaningful.

    name = name.replace(
        "'",
        ""
    )

    # Convert accented characters
    # into simpler comparison forms.

    replacements = {

        "á": "a",
        "à": "a",
        "â": "a",
        "ä": "a",
        "ã": "a",

        "é": "e",
        "è": "e",
        "ê": "e",
        "ë": "e",

        "í": "i",
        "ì": "i",
        "î": "i",
        "ï": "i",

        "ó": "o",
        "ò": "o",
        "ô": "o",
        "ö": "o",
        "õ": "o",

        "ú": "u",
        "ù": "u",
        "û": "u",
        "ü": "u",

        "ç": "c",

        "ñ": "n",

        "š": "s",
        "ş": "s",

        "ž": "z",
        "ź": "z",
        "ż": "z",

        "č": "c",

        "ğ": "g"
    }

    for old, new in replacements.items():

        name = name.replace(
            old,
            new
        )

    name = re.sub(
        r"[^a-z0-9 ]",
        "",
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    )

    return name.strip()


# ============================================================
# NAME MATCHING
# ============================================================

def names_match(
    player_name,
    wikidata_name,
    aliases
):

    normalized_player = (
        normalize_name(
            player_name
        )
    )

    normalized_wikidata = (
        normalize_name(
            wikidata_name
        )
    )

    # Exact normalized name

    if (
        normalized_player
        == normalized_wikidata
    ):

        return (
            True,
            "exact-name"
        )

    # Check aliases

    for alias in aliases:

        alias_name = (
            alias
            .get("value", "")
            if isinstance(
                alias,
                dict
            )
            else str(alias)
        )

        if (
            normalize_name(
                alias_name
            )
            == normalized_player
        ):

            return (
                True,
                "alias-name"
            )

    return (
        False,
        None
    )


# ============================================================
# GET ALIASES
# ============================================================

def get_aliases(entity):

    aliases = []

    if not entity:

        return aliases

    entity_aliases = entity.get(
        "aliases",
        {}
    )

    # English aliases first

    for alias in (
        entity_aliases
        .get("en", [])
    ):

        aliases.append(
            alias
        )

    return aliases


# ============================================================
# CHECK FOOTBALL RELEVANCE
# ============================================================

def football_relevance(
    entity
):

    if not entity:

        return False

    claims = get_claims(
        entity
    )

    # P106 = occupation

    occupation_ids = []

    for statement in claims.get(
        "P106",
        []
    ):

        mainsnak = statement.get(
            "mainsnak",
            {}
        )

        datavalue = mainsnak.get(
            "datavalue"
        )

        if not datavalue:

            continue

        value = datavalue.get(
            "value"
        )

        if isinstance(
            value,
            dict
        ):

            qid = value.get(
                "id"
            )

            if qid:

                occupation_ids.append(
                    qid
                )

    football_occupation_ids = {

        # football player
        "Q937857",

        # association football player
        "Q1145276"
    }

    if (
        football_occupation_ids
        & set(occupation_ids)
    ):

        return True

    description = (
        entity
        .get("descriptions", {})
        .get("en", {})
        .get("value", "")
        .lower()
    )

    football_words = [

        "footballer",
        "football player",
        "soccer player",
        "association football player",
        "football manager",
        "football coach"
    ]

    for word in football_words:

        if word in description:

            return True

    return False


# ============================================================
# DETERMINE SOURCE DATE PRECISION
# ============================================================

def source_date_precision(
    date_of_birth
):

    if not date_of_birth:

        return "unknown"

    parts = (
        date_of_birth
        .split("-")
    )

    if len(parts) != 3:

        return "unknown"

    year, month, day = parts

    if (
        month == "01"
        and day == "01"
    ):

        # OpenFootball frequently uses
        # January 1 when only the birth
        # year is available.
        #
        # We therefore initially treat
        # this as year precision.
        #
        # If Wikidata independently
        # confirms January 1 with day
        # precision, we can upgrade it.

        return "year"

    return "full"


# ============================================================
# DETERMINE FINAL DATE PRECISION
# ============================================================

def determine_final_precision(
    source_date,
    source_precision,
    wikidata_date,
    wikidata_precision
):

    if (
        wikidata_date
        and wikidata_precision
        == 11
    ):

        return "full"

    if (
        wikidata_date
        and wikidata_precision
        == 10
    ):

        return "month"

    if (
        wikidata_date
        and wikidata_precision
        == 9
    ):

        return "year"

    return source_precision


# ============================================================
# FIND BEST WIKIDATA MATCH
# ============================================================

def find_match(
    player
):

    name = player.get(
        "name",
        ""
    )

    source_dob = player.get(
        "dateOfBirth"
    )

    if not name:

        return None

    if not source_dob:

        return None

    source_precision = (
        source_date_precision(
            source_dob
        )
    )

    search_results = search_wikidata(
        name
    )

    if not search_results:

        return None

    candidates = []

    for result in search_results:

        qid = result.get(
            "id"
        )

        if not qid:

            continue

        entity = get_entity(
            qid
        )

        if not entity:

            continue

        claims = get_claims(
            entity
        )

        wikidata_dob = (
            get_best_date_claim(
                claims,
                "P569"
            )
        )

        if not wikidata_dob:

            continue

        wikidata_date = (
            wikidata_dob[
                "date"
            ]
        )

        wikidata_precision = (
            wikidata_dob.get(
                "precision"
            )
        )

        label = (
            entity
            .get("labels", {})
            .get("en", {})
            .get("value")
        )

        aliases = get_aliases(
            entity
        )

        name_ok, name_method = (
            names_match(
                name,
                label,
                aliases
            )
        )

        if not name_ok:

            continue

        # ----------------------------------------------------
        # DOB MATCHING
        # ----------------------------------------------------

        date_match = False

        date_method = None

        # ----------------------------------------------------
        # Case 1:
        # Source has a full date.
        # Wikidata must have the same full date.
        # ----------------------------------------------------

        if source_precision == "full":

            if (
                wikidata_date
                == source_dob
            ):

                date_match = True

                date_method = (
                    "exact-full-dob"
                )

        # ----------------------------------------------------
        # Case 2:
        # Source appears to contain only
        # the birth year.
        #
        # We allow Wikidata to have a
        # more precise date as long as
        # the year is the same.
        # ----------------------------------------------------

        elif source_precision == "year":

            source_year = (
                source_dob
                .split("-")[0]
            )

            wikidata_year = (
                wikidata_date
                .split("-")[0]
            )

            if (
                source_year
                == wikidata_year
            ):

                date_match = True

                if (
                    wikidata_precision
                    == 11
                ):

                    date_method = (
                        "year-to-full-dob"
                    )

                elif (
                    wikidata_precision
                    == 10
                ):

                    date_method = (
                        "year-to-month-dob"
                    )

                else:

                    date_method = (
                        "year-only-dob"
                    )

        if not date_match:

            continue

        # ----------------------------------------------------
        # FOOTBALL RELEVANCE
        # ----------------------------------------------------

        is_football_related = (
            football_relevance(
                entity
            )
        )

        # ----------------------------------------------------
        # SCORING
        # ----------------------------------------------------

        score = 0

        if (
            name_method
            == "exact-name"
        ):

            score += 50

        elif (
            name_method
            == "alias-name"
        ):

            score += 35

        if (
            date_method
            == "exact-full-dob"
        ):

            score += 50

        elif (
            date_method
            == "year-to-full-dob"
        ):

            score += 45

        elif (
            date_method
            == "year-to-month-dob"
        ):

            score += 35

        elif (
            date_method
            == "year-only-dob"
        ):

            score += 20

        if is_football_related:

            score += 25

        # ----------------------------------------------------
        # DESCRIPTION
        # ----------------------------------------------------

        description = (
            entity
            .get("descriptions", {})
            .get("en", {})
            .get("value")
        )

        candidates.append({

            "entity":
                entity,

            "qid":
                qid,

            "label":
                label,

            "wikidataDob":
                wikidata_date,

            "wikidataPrecision":
                wikidata_precision,

            "nameMethod":
                name_method,

            "dateMethod":
                date_method,

            "footballRelated":
                is_football_related,

            "score":
                score,

            "description":
                description
        })

    if not candidates:

        return None

    # Highest score first

    candidates.sort(
        key=lambda item:
            item["score"],
        reverse=True
    )

    best = candidates[0]

    # --------------------------------------------------------
    # SAFETY CHECK
    #
    # If this is only a year-level match and there is no
    # football relevance, do not automatically accept it.
    # --------------------------------------------------------

    if (
        best["dateMethod"]
        == "year-only-dob"
        and not best["footballRelated"]
    ):

        return None

    entity = best[
        "entity"
    ]

    claims = get_claims(
        entity
    )

    # --------------------------------------------------------
    # NATIONALITY
    # --------------------------------------------------------

    nationality_id = (
        get_claim_id(
            claims,
            "P27"
        )
    )

    nationality = (
        get_entity_label(
            nationality_id
        )
    )

    # --------------------------------------------------------
    # BIRTHPLACE
    # --------------------------------------------------------

    birthplace_id = (
        get_claim_id(
            claims,
            "P19"
        )
    )

    birthplace = (
        get_entity_label(
            birthplace_id
        )
    )

    # --------------------------------------------------------
    # GENDER
    # --------------------------------------------------------

    gender_id = (
        get_claim_id(
            claims,
            "P21"
        )
    )

    gender = (
        get_entity_label(
            gender_id
        )
    )

    # --------------------------------------------------------
    # IMAGE
    # --------------------------------------------------------

    image = get_claim_string(
        claims,
        "P18"
    )

    # --------------------------------------------------------
    # DEATH DATE
    # --------------------------------------------------------

    death_claim = (
        get_best_date_claim(
            claims,
            "P570"
        )
    )

    death_date = None

    death_precision = None

    if death_claim:

        death_date = (
            death_claim.get(
                "date"
            )
        )

        death_precision = (
            death_claim.get(
                "precision"
            )
        )

    # --------------------------------------------------------
    # FINAL DOB
    # --------------------------------------------------------

    final_dob = (
        best["wikidataDob"]
    )

    final_precision = (
        determine_final_precision(
            source_dob,
            source_precision,
            best["wikidataDob"],
            best["wikidataPrecision"]
        )
    )

    # --------------------------------------------------------
    # CAREER STATUS
    # --------------------------------------------------------

    if death_date:

        career_status = (
            "deceased"
        )

    else:

        career_status = (
            "unknown"
        )

    return {

        "wikidataId":
            best["qid"],

        "wikidataUrl":
            (
                "https://www.wikidata.org/wiki/"
                + best["qid"]
            ),

        "matchedName":
            best["label"],

        "matchScore":
            best["score"],

        "matchMethod":
            best["dateMethod"],

        "nameMatchMethod":
            best["nameMethod"],

        "footballRelated":
            best["footballRelated"],

        "description":
            best["description"],

        "sourceDateOfBirth":
            source_dob,

        "sourceDatePrecision":
            source_precision,

        "wikidataDateOfBirth":
            best["wikidataDob"],

        "wikidataDatePrecision":
            best["wikidataPrecision"],

        "finalDateOfBirth":
            final_dob,

        "finalDatePrecision":
            final_precision,

        "nationality":
            nationality,

        "nationalityId":
            nationality_id,

        "birthPlace":
            birthplace,

        "birthPlaceId":
            birthplace_id,

        "gender":
            gender,

        "genderId":
            gender_id,

        "photo":
            image,

        "deathDate":
            death_date,

        "deathDatePrecision":
            death_precision,

        "careerStatus":
            career_status
    }


# ============================================================
# ENRICH PLAYERS
# ============================================================

def enrich_players():

    if not INPUT_FILE.exists():

        raise FileNotFoundError(
            f"Could not find {INPUT_FILE}"
        )

    print(
        "Loading Fyucha player database..."
    )

    with INPUT_FILE.open(
        "r",
        encoding="utf-8"
    ) as file:

        players = json.load(
            file
        )

    print(
        f"Total players available: "
        f"{len(players):,}"
    )

    test_players = players[
        :TEST_LIMIT
    ]

    print(
        f"Testing enrichment on "
        f"{len(test_players)} players."
    )

    enriched_players = []

    match_records = []

    matched = 0

    not_matched = 0

    errors = 0

    full_dates = 0

    year_only_dates = 0

    month_dates = 0

    corrected_dates = 0

    deceased = 0

    for number, player in enumerate(
        test_players,
        start=1
    ):

        name = player.get(
            "name",
            "Unknown"
        )

        print()
        print(
            f"[{number}/{len(test_players)}] "
            f"Checking {name}"
        )

        updated_player = dict(
            player
        )

        try:

            match = find_match(
                player
            )

            if match:

                matched += 1

                updated_player[
                    "wikidata"
                ] = match

                # --------------------------------------------
                # FINAL DATE
                # --------------------------------------------

                final_dob = match.get(
                    "finalDateOfBirth"
                )

                final_precision = match.get(
                    "finalDatePrecision"
                )

                if final_dob:

                    updated_player[
                        "dateOfBirth"
                    ] = final_dob

                updated_player[
                    "dateOfBirthPrecision"
                ] = final_precision

                # --------------------------------------------
                # NATIONALITY
                # --------------------------------------------

                if match.get(
                    "nationality"
                ):

                    updated_player[
                        "nationality"
                    ] = match[
                        "nationality"
                    ]

                # --------------------------------------------
                # BIRTHPLACE
                # --------------------------------------------

                if match.get(
                    "birthPlace"
                ):

                    updated_player[
                        "birthPlace"
                    ] = match[
                        "birthPlace"
                    ]

                # --------------------------------------------
                # GENDER
                # --------------------------------------------

                if match.get(
                    "gender"
                ):

                    updated_player[
                        "gender"
                    ] = match[
                        "gender"
                    ]

                # --------------------------------------------
                # PHOTO
                # --------------------------------------------

                if match.get(
                    "photo"
                ):

                    updated_player[
                        "photo"
                    ] = match[
                        "photo"
                    ]

                # --------------------------------------------
                # BIRTHDAY INDEX FIELD
                # --------------------------------------------

                if (
                    final_dob
                    and final_precision
                    == "full"
                ):

                    updated_player[
                        "birthDay"
                    ] = final_dob[
                        5:10
                    ]

                    full_dates += 1

                elif (
                    final_precision
                    == "month"
                ):

                    updated_player[
                        "birthDay"
                    ] = None

                    month_dates += 1

                else:

                    updated_player[
                        "birthDay"
                    ] = None

                    year_only_dates += 1

                # --------------------------------------------
                # CHECK WHETHER WIKIDATA CORRECTED DOB
                # --------------------------------------------

                source_dob = match.get(
                    "sourceDateOfBirth"
                )

                wikidata_dob = match.get(
                    "wikidataDateOfBirth"
                )

                if (
                    source_dob
                    and wikidata_dob
                    and source_dob
                    != wikidata_dob
                ):

                    corrected_dates += 1

                # --------------------------------------------
                # DECEASED
                # --------------------------------------------

                if (
                    match.get(
                        "careerStatus"
                    )
                    == "deceased"
                ):

                    updated_player[
                        "careerStatus"
                    ] = "deceased"

                    deceased += 1

                # --------------------------------------------
                # MATCH REPORT
                # --------------------------------------------

                match_records.append({

                    "playerId":
                        player.get(
                            "id"
                        ),

                    "playerName":
                        name,

                    "sourceDateOfBirth":
                        source_dob,

                    "sourceDatePrecision":
                        match.get(
                            "sourceDatePrecision"
                        ),

                    "wikidataId":
                        match.get(
                            "wikidataId"
                        ),

                    "matchedName":
                        match.get(
                            "matchedName"
                        ),

                    "wikidataDateOfBirth":
                        match.get(
                            "wikidataDateOfBirth"
                        ),

                    "wikidataDatePrecision":
                        match.get(
                            "wikidataDatePrecision"
                        ),

                    "finalDateOfBirth":
                        match.get(
                            "finalDateOfBirth"
                        ),

                    "finalDatePrecision":
                        match.get(
                            "finalDatePrecision"
                        ),

                    "matchScore":
                        match.get(
                            "matchScore"
                        ),

                    "matchMethod":
                        match.get(
                            "matchMethod"
                        ),

                    "nameMatchMethod":
                        match.get(
                            "nameMatchMethod"
                        ),

                    "footballRelated":
                        match.get(
                            "footballRelated"
                        )
                })

                print(
                    f"  MATCHED: "
                    f"{match.get('wikidataId')}"
                )

                print(
                    f"  Name: "
                    f"{match.get('matchedName')}"
                )

                print(
                    f"  Source DOB: "
                    f"{source_dob}"
                )

                print(
                    f"  Wikidata DOB: "
                    f"{match.get('wikidataDateOfBirth')}"
                )

                print(
                    f"  Final DOB: "
                    f"{match.get('finalDateOfBirth')}"
                )

                print(
                    f"  Precision: "
                    f"{match.get('finalDatePrecision')}"
                )

                print(
                    f"  Method: "
                    f"{match.get('matchMethod')}"
                )

            else:

                not_matched += 1

                # ------------------------------------------------
                # If there was no verified match, preserve the
                # existing player data but classify likely
                # January 1 placeholders as year precision.
                # ------------------------------------------------

                existing_dob = player.get(
                    "dateOfBirth"
                )

                precision = (
                    source_date_precision(
                        existing_dob
                    )
                )

                updated_player[
                    "dateOfBirthPrecision"
                ] = precision

                if precision != "full":

                    updated_player[
                        "birthDay"
                    ] = None

                print(
                    "  No verified match"
                )

        except (
            HTTPError,
            URLError,
            TimeoutError
        ) as error:

            errors += 1

            print(
                f"  Network error: "
                f"{error}"
            )

            # Preserve existing data

            existing_dob = player.get(
                "dateOfBirth"
            )

            precision = (
                source_date_precision(
                    existing_dob
                )
            )

            updated_player[
                "dateOfBirthPrecision"
            ] = precision

            if precision != "full":

                updated_player[
                    "birthDay"
                ] = None

        except Exception as error:

            errors += 1

            print(
                f"  Error: "
                f"{error}"
            )

        # --------------------------------------------------------
        # Small delay between players
        # --------------------------------------------------------

        time.sleep(
            REQUEST_DELAY
        )

        enriched_players.append(
            updated_player
        )

    # ============================================================
    # SAVE ENRICHED DATABASE
    # ============================================================

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            enriched_players,
            file,
            ensure_ascii=False,
            indent=2
        )

    # ============================================================
    # REPORT
    # ============================================================

    report = {

        "version":
            "2.0",

        "testLimit":
            TEST_LIMIT,

        "playersTested":
            len(test_players),

        "matched":
            matched,

        "notMatched":
            not_matched,

        "errors":
            errors,

        "matchRate":
            round(
                (
                    matched
                    / len(test_players)
                    * 100
                ),
                2
            )
            if test_players
            else 0,

        "fullDateRecords":
            full_dates,

        "monthPrecisionRecords":
            month_dates,

        "yearPrecisionRecords":
            year_only_dates,

        "correctedDates":
            corrected_dates,

        "deceasedRecords":
            deceased,

        "matches":
            match_records
    }

    with MATCH_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2
        )

    # ============================================================
    # SUMMARY
    # ============================================================

    print()
    print("=" * 65)
    print(
        "FYUCHA WIKIDATA ENRICHMENT V2"
    )
    print("=" * 65)

    print(
        f"Players tested: "
        f"{len(test_players)}"
    )

    print(
        f"Verified matches: "
        f"{matched}"
    )

    print(
        f"No match: "
        f"{not_matched}"
    )

    print(
        f"Errors: "
        f"{errors}"
    )

    print(
        f"Match rate: "
        f"{report['matchRate']}%"
    )

    print()

    print(
        f"Full DOB records: "
        f"{full_dates}"
    )

    print(
        f"Month precision: "
        f"{month_dates}"
    )

    print(
        f"Year precision: "
        f"{year_only_dates}"
    )

    print(
        f"DOB corrections: "
        f"{corrected_dates}"
    )

    print(
        f"Deceased records: "
        f"{deceased}"
    )

    print()

    print(
        f"Created: "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Created: "
        f"{MATCH_FILE}"
    )

    print("=" * 65)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    enrich_players()
