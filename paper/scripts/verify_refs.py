#!/usr/bin/env python3
"""Reference verification (revision rule 2).

Input : refs/seeds.json, a list of {key, doi | arxiv | query, expect: {author, year, title}, note}
        expect.author = first author surname, expect.year = year, expect.title = words that must occur in the title.
Output: refs/resolved/<key>.json   raw metadata returned by Crossref or arXiv
        refs/verified.bib          BibTeX generated only from the resolved metadata (PASS entries)
        refs/verification_report.md  PASS / FAIL per entry with the resolved fields
Entries with kind "web" (data sheets, documentation, theses) are listed for a manual check of the URL and are
not written to the .bib automatically.
Requests are paced and send no personal identifiers.
"""
import json, os, re, sys, time, unicodedata, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REFS = os.path.join(HERE, "..", "refs")
UA = {"User-Agent": "jsc-revision-refcheck/1.0"}


def get(url, accept=None):
    h = dict(UA)
    if accept:
        h["Accept"] = accept
    for attempt in range(4):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40).read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    raise last


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())


def crossref_doi(doi):
    m = json.loads(get("https://api.crossref.org/works/" + urllib.parse.quote(doi)))["message"]
    return from_crossref(m)


def crossref_query(q):
    url = ("https://api.crossref.org/works?rows=3&query.bibliographic=" + urllib.parse.quote(q))
    items = json.loads(get(url))["message"]["items"]
    return from_crossref(items[0]) if items else None


def from_crossref(m):
    year = None
    for k in ("published-print", "published-online", "issued", "created"):
        if m.get(k, {}).get("date-parts") and m[k]["date-parts"][0][0]:
            year = m[k]["date-parts"][0][0]
            break
    authors = []
    for a in m.get("author", []):
        fam = a.get("family") or a.get("name") or ""
        giv = a.get("given") or ""
        # some preprint registrations repeat the names inside "given" (for example "Nasir Ali Nasir"): keep the
        # tokens that are not the family name, once each
        toks = []
        for t in giv.split():
            if t.lower() != fam.lower() and t not in toks:
                toks.append(t)
        if fam.islower():
            fam = fam.capitalize()
        authors.append((fam, " ".join(toks)))
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", (m.get("title") or [""])[0])).strip()
    return dict(source="crossref", doi=m.get("DOI"), title=title,
                venue=(m.get("container-title") or [""])[0], volume=m.get("volume"), issue=m.get("issue"),
                pages=m.get("page") or m.get("article-number"), year=year, authors=authors, type=m.get("type"),
                publisher=m.get("publisher"), url=m.get("URL"))


def arxiv(aid):
    x = get("https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(aid))
    ns = {"a": "http://www.w3.org/2005/Atom", "ar": "http://arxiv.org/schemas/atom"}
    e = ET.fromstring(x).find("a:entry", ns)
    if e is None or e.find("a:title", ns) is None:
        return None
    authors = []
    for a in e.findall("a:author", ns):
        name = a.find("a:name", ns).text.strip()
        parts = name.split()
        authors.append((parts[-1], " ".join(parts[:-1])))
    doi = e.find("ar:doi", ns)
    jr = e.find("ar:journal_ref", ns)
    return dict(source="arxiv", arxiv=aid, title=re.sub(r"\s+", " ", e.find("a:title", ns).text).strip(),
                year=int(e.find("a:published", ns).text[:4]), authors=authors,
                doi=doi.text if doi is not None else None, journal_ref=jr.text if jr is not None else None,
                url=f"https://arxiv.org/abs/{aid}")


def check(r, exp):
    problems = []
    if not r:
        return ["not found"]
    if exp.get("author"):
        fa = norm(r["authors"][0][0]) if r["authors"] else ""
        if norm(exp["author"]).strip() not in fa:
            problems.append(f"first author {r['authors'][0][0] if r['authors'] else None!r} != {exp['author']!r}")
    if exp.get("year") and r.get("year") != exp["year"]:
        problems.append(f"year {r.get('year')} != {exp['year']}")
    t = norm(r.get("title"))
    for w in exp.get("title", "").split():
        if norm(w).strip() and norm(w).strip() not in t:
            problems.append(f"title lacks {w!r}")
    return problems


def bibtex(key, r):
    def esc(s):
        return str(s).replace("&", r"\&")
    auth = " and ".join(f"{f}, {g}".strip(", ") for f, g in r["authors"]) or "{Unknown}"
    f = {"author": auth, "title": "{" + esc(r["title"]) + "}", "year": r["year"]}
    if r["source"] == "arxiv" and not r.get("venue"):
        typ = "article"
        f.update(journal=f"arXiv preprint arXiv:{r['arxiv']}", eprint=r["arxiv"], archivePrefix="arXiv", url=r["url"])
        if r.get("doi"):
            f["doi"] = r["doi"]
    else:
        ctype = r.get("type") or ""
        typ = {"journal-article": "article", "proceedings-article": "inproceedings", "book": "book",
               "book-chapter": "incollection", "monograph": "book"}.get(ctype, "misc")
        if typ == "article":
            f["journal"] = esc(r["venue"])
        elif typ in ("inproceedings", "incollection"):
            f["booktitle"] = esc(r["venue"])
        if typ == "book":
            f["publisher"] = esc(r.get("publisher") or "")
        if ctype == "posted-content":
            f["howpublished"] = "Research Square preprint" if "rs.3.rs" in (r.get("doi") or "") else "Preprint"
            if r.get("status"):
                f["note"] = esc(r["status"])          # journal status stated by the author (seeds override)
        for k in ("volume", "number", "pages"):
            v = r.get({"number": "issue"}.get(k, k))
            if v:
                f[k] = str(v).replace("-", "--")   # BibTeX range syntax; style prints the range
        f["doi"] = r["doi"]
    body = ",\n".join(f"  {k} = {{{v}}}" if not str(v).startswith("{") else f"  {k} = {v}" for k, v in f.items())
    return f"@{typ}{{{key},\n{body}\n}}\n"


def main():
    seeds = json.load(open(os.path.join(REFS, "seeds.json")))
    os.makedirs(os.path.join(REFS, "resolved"), exist_ok=True)
    rows, bib = [], []
    for s in seeds:
        key = s["key"]
        cache = os.path.join(REFS, "resolved", key + ".json")
        if s.get("kind") == "web":
            rows.append((key, "MANUAL", s.get("url", ""), s.get("note", ""), ""))
            continue
        if os.path.exists(cache) and "--refresh" not in sys.argv:
            r = json.load(open(cache))
        else:
            try:
                r = crossref_doi(s["doi"]) if s.get("doi") else arxiv(s["arxiv"]) if s.get("arxiv") else crossref_query(s["query"])
            except Exception as e:  # noqa: BLE001
                r = None
                s["error"] = str(e)
            json.dump(r, open(cache, "w"), indent=1)
            time.sleep(2)
        if r and s.get("override"):
            # documented correction of a resolved field (reason kept in the report)
            r = dict(r, **{k: v for k, v in s["override"].items() if k != "reason"})
            s["note"] = (s.get("note", "") + " override: " + s["override"]["reason"]).strip()
        probs = check(r, s.get("expect", {}))
        status = "PASS" if not probs else "FAIL"
        desc = "" if not r else (f"{r['authors'][0][0] if r['authors'] else ''} ({r.get('year')}) {r.get('title')} | "
                                 f"{r.get('venue') or r.get('journal_ref') or 'arXiv ' + str(r.get('arxiv'))} "
                                 f"{r.get('volume') or ''}:{r.get('pages') or ''} doi:{r.get('doi')}")
        rows.append((key, status, desc, "; ".join(probs), s.get("note", "")))
        if status == "PASS":
            bib.append(bibtex(key, r))
        print(f"{status:6s} {key:26s} {desc[:150]} {'; '.join(probs)}", flush=True)
    open(os.path.join(REFS, "verified.bib"), "w").write("% generated by scripts/verify_refs.py from resolved metadata\n\n" + "\n".join(bib))
    with open(os.path.join(REFS, "verification_report.md"), "w") as f:
        f.write("# Reference verification report\n\nGenerated by scripts/verify_refs.py on "
                + time.strftime("%Y-%m-%d") + ".\n\n| Key | Status | Resolved metadata | Problems | Note |\n|---|---|---|---|---|\n")
        for row in rows:
            f.write("| " + " | ".join(str(c).replace("|", "/") for c in row) + " |\n")
    n_fail = sum(r[1] == "FAIL" for r in rows)
    print(f"{len(rows)} entries, {n_fail} FAIL, {sum(r[1] == 'MANUAL' for r in rows)} manual")
    return n_fail


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
