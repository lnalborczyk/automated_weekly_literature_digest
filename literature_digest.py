import html
import os
import re
import time
from datetime import date, timedelta
from typing import Any

import requests

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "8"))
MAX_PAPERS = int(os.getenv("MAX_PAPERS", "12"))
MIN_RELEVANCE_SCORE = int(os.getenv("MIN_RELEVANCE_SCORE", "4"))

TOPICS = [
    "inner speech",
    "inner speech diversity",
    "anendophasia",
    "auditory verbal aphantasia",
    "endophasia",
]

# Retrieval vocabulary. These terms are intentionally broader than the
# final relevance criteria so that potentially useful papers are not missed.
SEARCH_TERMS = [
    '"inner speech"',
    '"inner speaking"',
    '"inner hearing"',
    '"inner voice"',
    '"verbal imagery"',
    '"auditory verbal imagery"',
    "anendophasia",
    '"auditory verbal aphantasia"',
    "endophasia",
]

# Weighted relevance vocabulary.
#
# Exact/core terms receive large weights. Broader neighbouring concepts
# receive smaller weights and generally need to co-occur with a core term
# to survive the threshold.
CORE_PHRASES = {
    "anendophasia": 10,
    "auditory verbal aphantasia": 10,
    "endophasia": 9,
    "inner speech diversity": 9,
    "inner speech": 7,
    "inner speaking": 6,
    "inner hearing": 6,
    "inner voice": 5,
}

SECONDARY_PHRASES = {
    "verbal imagery": 3,
    "auditory verbal imagery": 4,
    "covert speech": 4,
    "silent speech": 3,
    "speech imagery": 3,
}

DIVERSITY_PHRASES = {
    "individual differences": 3,
    "individual difference": 3,
    "variability": 2,
    "variation": 2,
    "diversity": 3,
    "phenomenology": 3,
    "phenomenological": 2,
    "absence": 2,
    "absent": 2,
    "reduced inner speech": 4,
    "development": 1,
    "developmental": 1,
}

# Papers that mention only broad adjacent topics should not be included unless
# they also contain at least one core/secondary phrase.
ADJACENT_ONLY_PHRASES = {
    "auditory imagery",
    "working memory",
    "hallucination",
    "hallucinations",
    "speech production",
    "language production",
    "predictive processing",
}

USER_AGENT = "inner-speech-literature-digest/2.0-free"
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "").strip()
OPENALEX_EMAIL = os.getenv("OPENALEX_EMAIL", NCBI_EMAIL).strip()
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalise_title(title: str) -> str:
    title = clean_text(title).lower()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def doi_key(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.lower().strip()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    return doi


def reconstruct_openalex_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positions = []
    for word, indices in index.items():
        for i in indices:
            positions.append((i, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def deduplicate(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_doi = set()
    seen_title = set()
    out = []

    # Prefer records with abstracts.
    papers = sorted(
        papers,
        key=lambda p: (bool(p.get("abstract")), p.get("date", "")),
        reverse=True,
    )

    for paper in papers:
        dkey = doi_key(paper.get("doi"))
        tkey = normalise_title(paper.get("title", ""))

        if dkey and dkey in seen_doi:
            continue
        if tkey and tkey in seen_title:
            continue

        if dkey:
            seen_doi.add(dkey)
        if tkey:
            seen_title.add(tkey)
        out.append(paper)

    return out


def phrase_count(text: str, phrase: str) -> int:
    # Word-boundary-like matching that is robust to punctuation.
    pattern = r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)"
    return len(re.findall(pattern, text.lower()))


# ---------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------

def fetch_pubmed(start_date: date, end_date: date) -> list[dict[str, Any]]:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    query = " OR ".join(f"{term}[Title/Abstract]" for term in SEARCH_TERMS)

    params = {
        "db": "pubmed",
        "term": query,
        "mindate": start_date.strftime("%Y/%m/%d"),
        "maxdate": end_date.strftime("%Y/%m/%d"),
        "datetype": "edat",
        "retmode": "json",
        "retmax": 200,
        "tool": "inner_speech_digest",
    }
    if NCBI_EMAIL:
        params["email"] = NCBI_EMAIL

    r = session.get(f"{base}/esearch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    pmids = r.json().get("esearchresult", {}).get("idlist", [])
    if not pmids:
        return []

    fetch_params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": "inner_speech_digest",
    }
    if NCBI_EMAIL:
        fetch_params["email"] = NCBI_EMAIL

    r = session.get(f"{base}/efetch.fcgi", params=fetch_params, timeout=60)
    r.raise_for_status()

    import xml.etree.ElementTree as ET
    root = ET.fromstring(r.text)

    papers = []
    for article in root.findall(".//PubmedArticle"):
        citation = article.find("MedlineCitation")
        art = citation.find("Article") if citation is not None else None
        if art is None:
            continue

        pmid = clean_text(citation.findtext("PMID"))
        title_el = art.find("ArticleTitle")
        title = clean_text("".join(title_el.itertext()) if title_el is not None else "")

        abstract_parts = []
        for a in art.findall(".//Abstract/AbstractText"):
            label = a.attrib.get("Label")
            txt = clean_text("".join(a.itertext()))
            if txt:
                abstract_parts.append(f"{label}: {txt}" if label else txt)
        abstract = " ".join(abstract_parts)

        authors = []
        for au in art.findall(".//AuthorList/Author"):
            collective = clean_text(au.findtext("CollectiveName"))
            if collective:
                authors.append(collective)
                continue
            last = clean_text(au.findtext("LastName"))
            initials = clean_text(au.findtext("Initials"))
            name = " ".join(x for x in [last, initials] if x)
            if name:
                authors.append(name)

        journal = clean_text(art.findtext(".//Journal/Title"))

        pubdate = ""
        date_el = art.find(".//Journal/JournalIssue/PubDate")
        if date_el is not None:
            year = clean_text(date_el.findtext("Year"))
            month = clean_text(date_el.findtext("Month"))
            day = clean_text(date_el.findtext("Day"))
            pubdate = "-".join(x for x in [year, month, day] if x)

        doi = None
        for aid in article.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if aid.attrib.get("IdType") == "doi":
                doi = clean_text(aid.text)
                break

        papers.append(
            {
                "source": "PubMed",
                "title": title,
                "authors": authors,
                "date": pubdate,
                "journal": journal,
                "abstract": abstract,
                "doi": doi,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            }
        )

    return papers


# ---------------------------------------------------------------------
# bioRxiv
# ---------------------------------------------------------------------

def fetch_biorxiv(start_date: date, end_date: date) -> list[dict[str, Any]]:
    papers = []
    cursor = 0

    while True:
        url = (
            "https://api.biorxiv.org/details/biorxiv/"
            f"{start_date.isoformat()}/{end_date.isoformat()}/{cursor}"
        )
        r = session.get(url, timeout=60)
        r.raise_for_status()
        payload = r.json()
        collection = payload.get("collection", [])

        for item in collection:
            title = clean_text(item.get("title"))
            abstract = clean_text(item.get("abstract"))
            haystack = f"{title} {abstract}".lower()

            raw_terms = [
                "inner speech",
                "inner speaking",
                "inner voice",
                "verbal imagery",
                "auditory verbal imagery",
                "anendophasia",
                "auditory verbal aphantasia",
                "endophasia",
            ]
            if not any(term in haystack for term in raw_terms):
                continue

            doi = clean_text(item.get("doi"))
            papers.append(
                {
                    "source": "bioRxiv",
                    "title": title,
                    "authors": [
                        clean_text(x)
                        for x in str(item.get("authors", "")).split(";")
                        if clean_text(x)
                    ],
                    "date": clean_text(item.get("date")),
                    "journal": clean_text(item.get("category")) or "bioRxiv",
                    "abstract": abstract,
                    "doi": doi,
                    "url": f"https://doi.org/{doi}" if doi else "",
                }
            )

        messages = payload.get("messages", [])
        total = 0
        if messages:
            try:
                total = int(messages[0].get("total", 0))
            except (TypeError, ValueError):
                total = 0

        cursor += len(collection)
        if not collection or (total and cursor >= total):
            break

        time.sleep(0.25)

    return papers


# ---------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------

def fetch_openalex(start_date: date, end_date: date) -> list[dict[str, Any]]:
    papers = []

    queries = [
        "inner speech",
        "anendophasia",
        "auditory verbal aphantasia",
        "endophasia",
        "auditory verbal imagery",
    ]

    for query in queries:
        cursor = "*"
        for _ in range(5):
            params = {
                "search": query,
                "filter": (
                    f"from_publication_date:{start_date.isoformat()},"
                    f"to_publication_date:{end_date.isoformat()}"
                ),
                "per-page": 100,
                "cursor": cursor,
            }
            if OPENALEX_EMAIL:
                params["mailto"] = OPENALEX_EMAIL

            r = session.get(
                "https://api.openalex.org/works",
                params=params,
                timeout=60,
            )
            r.raise_for_status()
            payload = r.json()

            for work in payload.get("results", []):
                title = clean_text(work.get("display_name"))
                abstract = clean_text(
                    reconstruct_openalex_abstract(
                        work.get("abstract_inverted_index")
                    )
                )
                haystack = f"{title} {abstract}".lower()

                raw_terms = [
                    "inner speech",
                    "inner speaking",
                    "inner voice",
                    "verbal imagery",
                    "auditory verbal imagery",
                    "anendophasia",
                    "auditory verbal aphantasia",
                    "endophasia",
                ]
                if not any(term in haystack for term in raw_terms):
                    continue

                authors = [
                    clean_text(a.get("author", {}).get("display_name"))
                    for a in work.get("authorships", [])
                ]
                authors = [a for a in authors if a]

                primary = work.get("primary_location") or {}
                source = primary.get("source") or {}
                journal = clean_text(source.get("display_name"))

                doi = clean_text(work.get("doi"))
                if doi:
                    doi = doi.replace("https://doi.org/", "")

                url = ""
                if doi:
                    url = f"https://doi.org/{doi}"
                elif primary.get("landing_page_url"):
                    url = primary["landing_page_url"]
                elif work.get("id"):
                    url = work["id"]

                papers.append(
                    {
                        "source": "OpenAlex",
                        "title": title,
                        "authors": authors,
                        "date": clean_text(work.get("publication_date")),
                        "journal": journal,
                        "abstract": abstract,
                        "doi": doi,
                        "url": url,
                    }
                )

            cursor = payload.get("meta", {}).get("next_cursor")
            if not cursor or not payload.get("results"):
                break

            time.sleep(0.15)

    return papers


# ---------------------------------------------------------------------
# Free rule-based relevance screening
# ---------------------------------------------------------------------

def score_paper(paper: dict[str, Any]) -> dict[str, Any]:
    title = clean_text(paper.get("title")).lower()
    abstract = clean_text(paper.get("abstract")).lower()

    core_score = 0
    secondary_score = 0
    diversity_score = 0
    matched = []

    # Title matches count more heavily than abstract matches.
    for phrase, weight in CORE_PHRASES.items():
        title_hits = phrase_count(title, phrase)
        abstract_hits = phrase_count(abstract, phrase)

        if title_hits:
            core_score += weight * 2
            matched.append(phrase)
        elif abstract_hits:
            core_score += weight
            matched.append(phrase)

    for phrase, weight in SECONDARY_PHRASES.items():
        title_hits = phrase_count(title, phrase)
        abstract_hits = phrase_count(abstract, phrase)

        if title_hits:
            secondary_score += weight * 2
            matched.append(phrase)
        elif abstract_hits:
            secondary_score += weight
            matched.append(phrase)

    for phrase, weight in DIVERSITY_PHRASES.items():
        title_hits = phrase_count(title, phrase)
        abstract_hits = phrase_count(abstract, phrase)

        if title_hits:
            diversity_score += weight * 2
            matched.append(phrase)
        elif abstract_hits:
            diversity_score += weight
            matched.append(phrase)

    # Diversity terms should boost genuine inner-speech papers, not unrelated
    # papers. Cap the diversity contribution if no core/secondary term matched.
    if core_score == 0 and secondary_score == 0:
        diversity_score = min(diversity_score, 1)

    score = core_score + secondary_score + diversity_score

    # Special boost for papers combining a core inner-speech term with
    # variability/diversity/phenomenology language.
    if core_score > 0 and diversity_score > 0:
        score += 3

    # Very broad neighbouring concepts should not pass on their own.
    if core_score == 0 and secondary_score == 0:
        score = 0

    matched = sorted(set(matched))

    if score >= 18:
        label = "highly relevant"
        stars = "★★★"
    elif score >= MIN_RELEVANCE_SCORE:
        label = "clearly relevant"
        stars = "★★"
    else:
        label = "below threshold"
        stars = ""

    paper = dict(paper)
    paper["relevance_score"] = score
    paper["relevance_label"] = label
    paper["stars"] = stars
    paper["matched_terms"] = matched
    return paper


def screen_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [score_paper(p) for p in papers]


# ---------------------------------------------------------------------
# Discord formatting
# ---------------------------------------------------------------------

def short_authors(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) <= 3:
        return ", ".join(authors)
    return f"{authors[0]}, {authors[1]} et al."


def paper_block(paper: dict[str, Any], rank: int) -> str:
    authors = short_authors(paper.get("authors", []))
    journal = paper.get("journal") or paper.get("source") or ""
    year = (paper.get("date") or "")[:4]
    meta = " · ".join(x for x in [authors, journal, year] if x)
    terms = ", ".join(paper.get("matched_terms", []))

    lines = [
        f"**{rank}. {paper['title']}**  {paper.get('stars', '')}",
    ]
    if meta:
        lines.append(meta)
    if terms:
        lines.append(f"Matched: *{terms}*")
    if paper.get("url"):
        lines.append(f"<{paper['url']}>")

    return "\n".join(lines)


def chunk_for_discord(header: str, blocks: list[str], limit: int = 1900) -> list[str]:
    chunks = []
    current = header

    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block

    if current:
        chunks.append(current)
    return chunks


def post_discord(messages: list[str]) -> None:
    for message in messages:
        r = session.post(
            DISCORD_WEBHOOK_URL,
            json={
                "content": message,
                "allowed_mentions": {"parse": []},
            },
            timeout=60,
        )
        r.raise_for_status()
        time.sleep(0.5)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    today = date.today()
    start_date = today - timedelta(days=LOOKBACK_DAYS)

    print(f"Searching {start_date} -> {today}")

    all_papers = []
    all_papers.extend(fetch_pubmed(start_date, today))
    all_papers.extend(fetch_biorxiv(start_date, today))
    all_papers.extend(fetch_openalex(start_date, today))

    print(f"Retrieved before deduplication: {len(all_papers)}")

    unique = deduplicate(all_papers)
    print(f"After deduplication: {len(unique)}")

    screened = screen_papers(unique)

    relevant = [
        p for p in screened
        if p.get("relevance_score", 0) >= MIN_RELEVANCE_SCORE
    ]

    relevant.sort(
        key=lambda p: (
            p.get("relevance_score", 0),
            p.get("date", ""),
        ),
        reverse=True,
    )
    relevant = relevant[:MAX_PAPERS]

    date_range = f"{start_date.isoformat()} → {today.isoformat()}"

    if not relevant:
        post_discord([
            (
                "📚 **Weekly literature digest — Inner speech**\n"
                f"{date_range}\n\n"
                f"{len(unique)} candidate papers screened; "
                "none met the relevance threshold this week."
            )
        ])
        return

    blocks = [
        paper_block(paper, rank)
        for rank, paper in enumerate(relevant, start=1)
    ]

    header = (
        "📚 **Weekly literature digest — Inner speech / endophasia**\n"
        f"{date_range}\n"
        f"{len(unique)} candidate papers screened · "
        f"{len(relevant)} retained\n"
        "★★★ highly relevant · ★★ clearly relevant\n"
    )

    messages = chunk_for_discord(header, blocks)
    post_discord(messages)

    print(f"Posted {len(relevant)} papers in {len(messages)} Discord message(s).")


if __name__ == "__main__":
    main()
