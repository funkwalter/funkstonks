"""Property-based consistency fuzz for MoneyFlow dashboard / transactions / jahr.

Seeded random data -> for every period: Sankey conservation, node drilldowns,
KPI tiles, category table, /jahr rows + matrix, cat=none, excluded/transfer.
Never touches the real DB (DB_PATH is re-pointed to a temp file per seed).
"""

import html as htmlmod
import json
import os
import random
import re
import sys
import tempfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _harness import check, finish  # noqa: E402

import app as m


def test_consistency_fuzz():

    SEEDS = [int(x) for x in sys.argv[1:]] or list(range(1, 16))
    VERBOSE = os.environ.get("FUZZ_VERBOSE") == "1"

    m.app.config["TESTING"] = True
    m.app.secret_key = "t"

    # --------------------------------------------------------------- reporting
    violations = []  # (seed, period, what, expected, actual)
    observations = defaultdict(int)
    stats = defaultdict(int)

    def viol(seed, period, what, expected, actual):
        violations.append((seed, period, what, expected, actual))
        if VERBOSE:
            print("  VIOL seed=%s %s :: %s  exp=%s act=%s" % (seed, period, what, expected, actual))

    # --------------------------------------------------------------- HTML parsing
    ROW_RE = re.compile(r'<tr class="([^"]*)">(.*?)</tr>', re.S)
    AMT_RE = re.compile(
        r'<td class="num (?:neg|pos)">(-?[\d.]+)</td>\s*<td class="num">(?:<b>(-?[\d.]+)</b>)?</td>', re.S
    )
    SHARE_RE = re.compile(r"davon\s*(-?[\d.]+) € hier")
    TXID_RE = re.compile(r'data-tx-id="(\d+)"')
    SEL_RE = re.compile(r'<option value="(\d+)" selected>')
    DATE_RE = re.compile(r'<td class="num">(\d{4}-\d{2}-\d{2})</td>')

    def strip_tags(s):
        s = htmlmod.unescape(re.sub(r"<[^>]+>", "", s))
        for ch in ("↳", "▸", "▾"):
            s = s.replace(ch, "")
        return re.sub(r"\s+", " ", s).strip()

    def GET(c, url):
        """GET that turns a propagated exception into a pseudo-500."""
        try:
            return c.get(url)
        except Exception as e:

            class R:
                pass

            r = R()
            r.status_code = "EXC:" + repr(e)[:120]
            r.get_data = lambda as_text=True: ""
            return r

    def parse_list(c, url):
        """Rows of a /transactions page as dicts."""
        r = GET(c, url)
        stats["drilldowns"] += 1
        if r.status_code != 200:
            return None, r.status_code
        d = r.get_data(as_text=True)
        rows = []
        for style, body in ROW_RE.findall(d):
            am = AMT_RE.search(body)
            if not am:
                continue
            sh = SHARE_RE.search(body)
            tid = TXID_RE.search(body)
            sel = SEL_RE.search(body)
            dt = DATE_RE.search(body)
            rows.append(
                {
                    "amount": float(am.group(1)),
                    "eff": float(am.group(2)) if am.group(2) else None,
                    "share": float(sh.group(1)) if sh else None,
                    "split": "÷ aufgeteilt" in body,
                    "dimmed": "dim" in style,
                    "id": int(tid.group(1)) if tid else None,
                    "cat": int(sel.group(1)) if sel else None,
                    "date": dt.group(1) if dt else None,
                }
            )
        truncated = "Nur die 800 neuesten" in d
        return rows, truncated

    def counted_sum(rows, split_fix=None):
        """Sum of what the page shows for counting rows (non-dimmed).
        Split rows: 'davon X € hier' if present, else full amount (or split_fix(id))."""
        tot = 0.0
        for r in rows:
            if r["dimmed"]:
                continue
            if r["split"]:
                if r["share"] is not None:
                    tot += r["share"]
                elif split_fix:
                    tot += split_fix(r["id"])
                else:
                    tot += r["amount"]
            else:
                tot += r["eff"] if r["eff"] is not None else r["amount"]
        return tot

    def sankey_from_page(h):
        sn = json.loads(re.search(r'id="sankey-data"[^>]*>(\{.*?\})</script>', h, re.S).group(1))
        # the click handler lives in static/js/dashboard.js and reads the period
        # query string from the JSON block's data attribute (HTML-escaped there)
        js = re.search(r'id="sankey-data" data-period-qs="([^"]*)"', h)
        return sn, (htmlmod.unescape(js.group(1)) if js else None)

    def label_value(lab):
        mo = re.search(r"\(([+\-]?)([\d.]+) € · ", lab)
        if not mo:
            return None
        return float(mo.group(2))

    def tile_api(c, period_url, title):
        """Dashboard tile via /api/period: (drilldown URL, value) - the same
        calculation as the page, without HTML regexes."""
        key = {"Einnahmen": "inc", "Ausgaben": "exp", "Saldo": "saldo", "Investiert": "invest"}[title]
        q = period_url.split("?", 1)[1] if "?" in period_url else ""
        r = c.get("/api/period" + ("?" + q if q else ""))
        if r.status_code != 200:
            return None, None
        t = r.get_json()["tiles"][key]
        return t["url"], float(t["value"])

    def tile(h, title):
        mo = re.search(
            r'<div class="kpi"><span>' + title + r'[^<]*</span>\s*<a href="([^"]+)"[^>]*>\s*<b[^>]*>\s*(-?[\d.]+) €', h
        )
        if not mo:
            return None, None
        return htmlmod.unescape(mo.group(1)), float(mo.group(2))

    def table_rows(seg):
        """Rows of a category table: list of dict(name, amount, href, sub, key)."""
        out = []
        for cls, key, body in re.findall(r'<tr(?: class="sub-(\w+)"( hidden)?)?>(.*?)</tr>', seg, re.S):
            if "<td" not in body or "<b>Summe</b>" in body:
                continue
            tds = re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)
            name = strip_tags(tds[0])
            am = re.search(r'<td class="num(?: mute)?">(-?[\d.]+) €</td>', body)
            href = re.search(r'href="([^"]+)"', tds[0])
            out.append(
                {
                    "name": name,
                    "amount": float(am.group(1)) if am else None,
                    "href": htmlmod.unescape(href.group(1)) if href else None,
                    "sub": cls or None,
                    "partner": bool(href and "q=" in href.group(1)),
                }
            )
        return out

    # --------------------------------------------------------------- generator
    def gen(seed):
        rng = random.Random(seed)
        tmp = tempfile.mkdtemp(prefix="mf_fuzz_%d_" % seed)
        m.DB_PATH = os.path.join(tmp, "t.db")
        m.init_db()
        con = m.db()
        m.set_setting(con, "setup_done", "1")
        con.commit()

        cats = {r["name"]: dict(r) for r in con.execute("SELECT * FROM categories")}

        def cid(n):
            return cats[n]["id"]

        # Investitionen top-level expense
        con.execute("INSERT OR IGNORE INTO categories(name,color,kind) VALUES('Investitionen','#123456','expense')")
        # subcategories
        parents = rng.sample(
            ["Lebensmittel", "Lifestyle", "Restaurant/Ausgehen", "Wohnung", "Konsum", "Abos"], rng.randint(2, 4)
        )
        if rng.random() < 0.6:
            parents.append("Investitionen")
        cats = {r["name"]: dict(r) for r in con.execute("SELECT * FROM categories")}
        subs = {}
        for p in parents:
            for i in range(rng.randint(1, 3)):
                n = "%s-Sub%d" % (p, i + 1)
                con.execute(
                    "INSERT INTO categories(name,color,kind,parent_id) VALUES(?,?,?,?)",
                    (n, "#%06x" % rng.randint(0, 0xFFFFFF), "expense", cid(p)),
                )
                subs.setdefault(p, []).append(n)
        con.commit()
        cats = {r["name"]: dict(r) for r in con.execute("SELECT * FROM categories")}
        kind_of = {r["id"]: r["kind"] for r in cats.values()}
        parent_of = {r["id"]: r["parent_id"] for r in cats.values()}
        exp_cats = [r["id"] for r in cats.values() if r["kind"] == "expense"]
        inc_cats = [r["id"] for r in cats.values() if r["kind"] == "income"]
        tr_cats = [r["id"] for r in cats.values() if r["kind"] == "transfer"]
        inv = cid("Investitionen")
        inv_sub = cid(subs["Investitionen"][0]) if "Investitionen" in subs else None
        woh = cid("Wohnung")

        # months
        y0 = rng.choice([2024, 2025])
        span = rng.randint(24, 36)
        all_months = []
        y, mo = y0, 1
        for _ in range(span):
            all_months.append("%04d-%02d" % (y, mo))
            mo += 1
            if mo > 12:
                y, mo = y + 1, 1
        populated = sorted(rng.sample(all_months, max(4, int(len(all_months) * rng.uniform(0.45, 0.8)))))
        # fixed partner: contiguous range, all populated
        fx_start = rng.randint(0, len(all_months) - 6)
        fx_len = rng.randint(3, 8)
        fx_months = all_months[fx_start : fx_start + fx_len]
        populated = sorted(set(populated) | set(fx_months))
        with_pos_uncat = seed % 2 == 0
        with_probe = seed % 3 == 0

        rows = []  # independent model

        def add(
            date, partner, amount, cat, status="active", eff_amt=None, source=None, splits=None, intended=None, desc="x"
        ):
            source = source or rng.choice(["bank", "bank", "paypal", "card"])
            con.execute(
                """INSERT INTO transactions(source,booking_date,partner,description,amount,
            currency,raw_hash,status,category_id,effective_amount) VALUES(?,?,?,?,?,'EUR',?,?,?,?)""",
                (source, date, partner, desc, amount, "h%d_%d" % (seed, len(rows)), status, cat, eff_amt),
            )
            tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            if splits:
                for c_, a_ in splits:
                    con.execute("INSERT INTO splits(tx_id,category_id,amount) VALUES(?,?,?)", (tid, c_, a_))
            rows.append(
                {
                    "id": tid,
                    "date": date,
                    "month": intended or date[:7],
                    "partner": partner,
                    "amount": amount,
                    "eff": eff_amt,
                    "cat": cat,
                    "status": status,
                    "splits": splits or [],
                    "source": source,
                }
            )
            return tid

        def money(lo, hi):
            return round(rng.uniform(lo, hi), 2)

        def day(mon, d=None):
            return "%s-%02d" % (mon, d or rng.randint(1, 27))

        def status():
            return "excluded" if rng.random() < 0.10 else "active"

        for mon in populated:
            # income
            if rng.random() > 0.10:
                for _ in range(rng.randint(1, 2)):
                    add(day(mon), "Arbeitgeber", money(1500, 4000), rng.choice(inc_cats), status())
            # expenses
            for _ in range(rng.randint(5, 15)):
                cat = rng.choice(exp_cats)
                r = rng.random()
                amt = -money(1, 800)
                effv = None
                if r < 0.15:
                    cat = None  # uncategorized
                elif r < 0.25:
                    effv = round(amt * rng.uniform(0.3, 0.9), 2)  # own share
                elif r < 0.33:
                    amt = money(5, 300)  # refund into expense cat
                add(day(mon), "Partner%d" % rng.randint(1, 40), amt, cat, status(), effv)
            if with_pos_uncat and rng.random() < 0.5:
                add(day(mon), "Unbekannt", money(10, 400), None, status())
            # net-positive category candidate: big refund
            if rng.random() < 0.25:
                add(day(mon), "Erstattung", money(500, 2000), rng.choice(exp_cats), status())
            # transfers
            for _ in range(rng.randint(1, 3)):
                add(day(mon), "Umbuchung", rng.choice([-1, 1]) * money(50, 2000), rng.choice(tr_cats), status())
            # investitionen
            if rng.random() < 0.7:
                add(day(mon), "Broker", -money(100, 500), inv, status())
            if inv_sub and rng.random() < 0.5:
                add(day(mon), "Broker Sub", -money(50, 300), inv_sub, status())
            # splits
            for _ in range(rng.randint(0, 2)):
                cents = rng.randint(2000, 60000)
                nparts = rng.randint(2, 3)
                cuts = sorted(rng.sample(range(1, cents), nparts - 1))
                parts = [b - a for a, b in zip([0] + cuts, cuts + [cents])]
                pool = exp_cats + tr_cats
                pcats = [rng.choice(exp_cats)] + [rng.choice(pool) for _ in range(nparts - 1)]
                sp = [(c_, -p / 100.0) for c_, p in zip(pcats, parts)]
                add(day(mon), "Bankomat", -cents / 100.0, None, status(), None, splits=sp)

        # fixed partner with smoothing drift
        for i, mon in enumerate(fx_months):
            amt = -money(900, 1100)
            if rng.random() < 0.5:
                date = mon + "-28"
            else:
                nxt = all_months[fx_start + i + 1] if fx_start + i + 1 < len(all_months) else None
                if nxt is None:
                    date = mon + "-28"
                else:
                    date = nxt + "-02"
            add(
                date,
                "Hausverwaltung Fuzz KG",
                amt,
                woh,
                "active",
                round(amt / 2, 2),
                source="bank",
                intended=mon,
                desc="Miete",
            )
        con.execute(
            "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES(?,?,?,?)",
            ("Hausverwaltung", woh, 50, ""),
        )
        # The app cannot know which month a rent debit is "for"; its convention is
        # slot_i = month(first booking) + i. The model follows the same convention so
        # that only genuine inconsistencies remain.
        fx_rows = sorted([r for r in rows if r["partner"] == "Hausverwaltung Fuzz KG"], key=lambda r: r["date"])
        y_, m_ = int(fx_rows[0]["date"][:4]), int(fx_rows[0]["date"][5:7])
        for r in fx_rows:
            r["month"] = "%04d-%02d" % (y_, m_)
            m_ += 1
            if m_ > 12:
                y_, m_ = y_ + 1, 1
        if with_probe:
            # second fixed pattern with exactly ONE booking (nothing to smooth)
            pm = rng.choice(populated)
            add(day(pm, 15), "Energie Fuzz AG", -120.0, woh, "active", -60.0, source="bank", desc="Strom")
            con.execute(
                "INSERT INTO fixed_costs(pattern,category_id,share_percent,sw_keyword) VALUES(?,?,?,?)",
                ("Energie Fuzz", woh, 50, ""),
            )
        con.commit()
        con.close()
        name_of = {r["id"]: n for n, r in cats.items()}
        return {
            "rows": rows,
            "kind_of": kind_of,
            "parent_of": parent_of,
            "name_of": name_of,
            "cats": cats,
            "inv": inv,
            "inv_sub": inv_sub,
            "woh": woh,
            "populated": populated,
            "fx_months": fx_months,
            "with_probe": with_probe,
            "with_pos_uncat": with_pos_uncat,
        }

    # --------------------------------------------------------------- independent model
    def model_net(G, months=None, year=None, use_intended=True):
        """net[cat_id or None] = signed sum over active counting parts.
        Returns (net_by_cat, has_pos_uncat, has_neg_uncat)."""
        net = defaultdict(float)
        uncat_pos = uncat_neg = 0.0
        for r in G["rows"]:
            if r["status"] != "active":
                continue
            mon = r["month"] if use_intended else r["date"][:7]
            if months is not None and mon not in months:
                continue
            if year is not None and mon[:4] != year:
                continue
            if r["splits"]:
                for c_, a_ in r["splits"]:
                    if G["kind_of"][c_] == "transfer":
                        continue
                    net[c_] += a_
            else:
                v = r["eff"] if r["eff"] is not None else r["amount"]
                if r["cat"] is None:
                    if v > 0:
                        uncat_pos += v
                    else:
                        uncat_neg += v
                    continue
                if G["kind_of"][r["cat"]] == "transfer":
                    continue
                net[r["cat"]] += v
        return net, uncat_pos, uncat_neg

    def model_summary(G, months=None, year=None, use_intended=True):
        """App-like summary: exp[name]=positive net expense, inc[name]=income, hidden[name]=net refund."""
        net, up, un = model_net(G, months, year, use_intended)
        exp, inc, hidden = {}, {}, {}
        for c_, v in net.items():
            if abs(v) < 0.004:
                continue
            k = G["kind_of"][c_]
            n = G["name_of"][c_]
            if k == "income":
                inc[n] = inc.get(n, 0) + v
            elif -v > 0.004:
                exp[n] = -v
            else:
                hidden[n] = v
        # Since 2026-09-11 "Unkategorisiert" is ONE net pot (like a category):
        # net expense -> exp, net inflow -> inc.
        net_un = up + un  # up >= 0, un <= 0
        if -net_un > 0.004:
            exp["Unkategorisiert"] = -net_un
        elif net_un > 0.004:
            inc["Unkategorisiert"] = net_un
        return exp, inc, hidden

    def top_name(G, name):
        """Top-level category of a category name in the model."""
        c_ = next((i for i, n in G["name_of"].items() if n == name), None)
        while c_ is not None and G["parent_of"].get(c_):
            c_ = G["parent_of"][c_]
        return G["name_of"].get(c_, name)

    def spend(G, exp):
        """Expenses WITHOUT investments - that is what the tile shows since 2026-10-13
        (investments have their own tile and are no longer in the category table;
        the Sankey branch "Ausgaben" never included them)."""
        return sum(v for n, v in exp.items() if top_name(G, n) != "Investitionen")

    # --------------------------------------------------------------- checks
    def approx(a, b, tol=0.02):
        return abs(a - b) <= tol

    def check_dashboard(seed, G, c, period_url, pname):
        r = GET(c, period_url)
        stats["periods"] += 1
        if r.status_code != 200:
            viol(seed, pname, "dashboard status", 200, r.status_code)
            return
        h = r.get_data(as_text=True)
        sn, qs = sankey_from_page(h)
        if qs is None:
            viol(seed, pname, "click-handler qs missing", "present", None)
            return
        if "&amp;" in qs:
            viol(seed, pname, "&amp; in JS click url", "&", qs)
        n = len(sn["labels"])
        inflow = [0.0] * n
        outflow = [0.0] * n
        nin = [0] * n
        nout = [0] * n
        for s_, t_, v in zip(sn["source"], sn["target"], sn["value"]):
            outflow[s_] += v
            inflow[t_] += v
            nout[s_] += 1
            nin[t_] += 1
        # 1) conservation
        for i, lab in enumerate(sn["labels"]):
            stats["nodes"] += 1
            tol = 0.02 + 0.005 * (nin[i] + nout[i])
            if nin[i] and nout[i] and not approx(inflow[i], outflow[i], tol):
                viol(seed, pname, "conservation " + lab[:40], round(inflow[i], 2), round(outflow[i], 2))
            lv = label_value(lab)
            if lv is not None:
                if nin[i] and not approx(lv, inflow[i], tol):
                    viol(seed, pname, "label!=inflow " + lab[:40], lv, round(inflow[i], 2))
                elif not nin[i] and nout[i] and not approx(lv, outflow[i], tol):
                    # source node (income) - outflow > label happens when saldo < 0
                    if sn["node_col"][i] == 0 and outflow[i] > lv:
                        observations["income node outflow > label (deficit period, Sankey cannot show)"] += 1
                    else:
                        viol(seed, pname, "label!=outflow " + lab[:40], lv, round(outflow[i], 2))
        # 2) clickable nodes
        for i, lab in enumerate(sn["labels"]):
            f = sn["filter"][i]
            if not f:
                continue
            stats["clickable"] += 1
            # the number the user sees is the label; flows only as fallback
            val = label_value(lab)
            if val is None:
                val = inflow[i] if nin[i] else outflow[i]
            rows, trunc = parse_list(c, "/transactions?%s&cat=%s" % (qs, f))
            if rows is None:
                viol(seed, pname, "node drill status " + lab[:40], 200, trunc)
                continue
            if trunc:
                observations["drilldown truncated at 800 rows"] += 1
            tot = -counted_sum(rows)
            if not approx(abs(tot), val, 0.02) or (val > 0.02 and not rows):
                tag = "node drill " + lab[:45] + " [cat=" + str(f)[:30] + "]"
                viol(seed, pname, tag, round(val, 2), round(abs(tot), 2))
            if any(r["dimmed"] for r in rows):
                observations["drilldown list contains dimmed (excluded) rows"] += 1
            if any(r["split"] and r["share"] is None and not r["dimmed"] for r in rows):
                observations["split row without 'davon' share in cat-filtered list"] += 1
        # 3) tiles
        for title in ("Einnahmen", "Ausgaben", "Saldo"):
            url, kpi = tile_api(c, period_url, title)
            if url is None:
                viol(seed, pname, "tile missing " + title, "present", None)
                continue
            stats["tiles"] += 1
            rows, trunc = parse_list(c, url)
            if rows is None:
                viol(seed, pname, "tile drill status " + title, 200, trunc)
                continue
            if title == "Saldo":
                # the offered link lists everything in the period, transfers included -
                # sum only non-transfer rows; split rows carry no share here, so use the
                # model to take only the non-transfer parts
                by_id = {r["id"]: r for r in G["rows"]}

                def fix(tid, by_id=by_id):
                    return sum(a for c_, a in by_id[tid]["splits"] if G["kind_of"][c_] != "transfer")

                tot = sum(
                    (r["eff"] if r["eff"] is not None else r["amount"]) if not r["split"] else fix(r["id"])
                    for r in rows
                    if not r["dimmed"] and not (r["cat"] and G["kind_of"].get(r["cat"]) == "transfer")
                )
                _e, _i, _h = model_summary(G, months=_period_months(qs))
                true_net = sum(_i.values()) - sum(_e.values()) + sum(_h.values())
                if not approx(kpi, true_net, 0.02) and approx(kpi + sum(_h.values()), true_net, 0.02):
                    observations["tile Saldo ignores net-positive categories (real net = tile + Σ net-positive)"] += 1
                probe_loss = sum(
                    (r["eff"] if r["eff"] is not None else r["amount"])
                    for r in G["rows"]
                    if r["partner"] == "Energie Fuzz AG"
                    and r["status"] == "active"
                    and r["month"] in _period_months(qs)
                )
                if not approx(tot, true_net, 0.02):
                    if approx(tot + probe_loss, true_net, 0.02):
                        observations["Saldo link list misses single-booking fixed-cost partner (probe)"] += 1
                    else:
                        viol(
                            seed,
                            pname,
                            "tile Saldo link list != real net (link=%s)" % url,
                            round(true_net, 2),
                            round(tot, 2),
                        )
                # Sankey 'Saldo' child vs tile Saldo
                for i, lab in enumerate(sn["labels"]):
                    if lab.startswith("Saldo ("):
                        sv = label_value(lab)
                        if not approx(sv, kpi, 0.02):
                            observations["Sankey 'Saldo' node != Saldo tile (differs by Σ net-positive cats)"] += 1
            else:
                tot = counted_sum(rows)
                tot = tot if title == "Einnahmen" else -tot
                if not approx(tot, kpi, 0.02):
                    viol(seed, pname, "tile %s (link=%s)" % (title, url), kpi, round(tot, 2))
        # 4) category table
        seg = h[h.index("Ausgaben nach Kategorie") :]
        seg = seg[: seg.index("</table>")]
        trs = table_rows(seg)
        total_exp = float(re.search(r'<b>Summe</b></td><td class="num"><b>(-?[\d.]+) €', seg).group(1))
        parent = None
        children_sum = 0.0
        nchild = 0
        table_sum = 0.0

        def close_parent():
            if parent is not None and nchild:
                stats["table_parents"] += 1
                if not approx(parent["amount"], children_sum, 0.01 * nchild + 0.01):
                    viol(
                        seed,
                        pname,
                        "table parent!=children " + parent["name"],
                        parent["amount"],
                        round(children_sum, 2),
                    )

        for tr in trs:
            stats["table_rows"] += 1
            if tr["sub"]:
                children_sum += tr["amount"]
                nchild += 1
            else:
                close_parent()
                parent = tr
                children_sum = 0.0
                nchild = 0
                table_sum += tr["amount"]
            if tr["href"]:
                rows, trunc = parse_list(c, tr["href"])
                if rows is None:
                    viol(seed, pname, "table drill status " + tr["name"], 200, trunc)
                    continue
                tot = -counted_sum(rows)
                if not approx(tot, tr["amount"], 0.02):
                    viol(
                        seed,
                        pname,
                        "table row drill %s [%s]" % (tr["name"], tr["href"].split("&cat=")[-1]),
                        tr["amount"],
                        round(tot, 2),
                    )
            elif not tr["sub"] and tr["name"] == "Unkategorisiert":
                observations["table row 'Unkategorisiert' has no link"] += 1
        close_parent()
        if not approx(table_sum, total_exp, 0.01 * len(trs) + 0.01):
            viol(seed, pname, "table top rows != Summe", total_exp, round(table_sum, 2))
        # model comparison for the period (dashboard uses intended/smoothed months)
        # period months as the page's own click-URL describes them (gap months have
        # no rows anyway, so months_between == the real months for the model)
        if qs.startswith("month="):
            sel_months = {qs[6:]}
        else:
            f_, t_ = re.search(r"from=([\d-]+)&to=([\d-]+)", qs).groups()
            sel_months = set(m.months_between(f_, t_))
        exp_m, inc_m, hid_m = model_summary(G, months=sel_months)
        _, kpi_e = tile_api(c, period_url, "Ausgaben")
        _, kpi_i = tile_api(c, period_url, "Einnahmen")
        if not approx(spend(G, exp_m), kpi_e, 0.02):
            viol(seed, pname, "model total_exp != Ausgaben tile", round(spend(G, exp_m), 2), kpi_e)
        if not approx(sum(inc_m.values()), kpi_i, 0.02):
            viol(seed, pname, "model total_inc != Einnahmen tile", round(sum(inc_m.values()), 2), kpi_i)
        if "range=all" in pname:
            # nothing may be missing from "alles zusammen"
            e_all, i_all, h_all = model_summary(G, months=None)
            if not approx(spend(G, e_all), kpi_e, 0.02):
                observations[
                    "range=all omits bookings (smoothed into a month without other bookings): %.2f"
                    % (sum(e_all.values()) - kpi_e)
                ] += 1
        # per-category model vs table: leaves = sub rows, or top rows without subs
        leaves = {}
        i = 0
        while i < len(trs):
            tr = trs[i]
            if tr["sub"]:
                i += 1
                continue
            has_sub = i + 1 < len(trs) and trs[i + 1]["sub"]
            if not has_sub:
                leaves[tr["name"]] = tr["amount"]
            else:
                j = i + 1
                while j < len(trs) and trs[j]["sub"]:
                    nm = tr["name"] if trs[j]["name"] == "ohne Unterkategorie" else trs[j]["name"]
                    leaves[nm] = trs[j]["amount"]
                    j += 1
            i += 1
        for nm, v in exp_m.items():
            if top_name(G, nm) == "Investitionen":
                continue  # own tile/page, deliberately not in the expense table (2026-10-13)
            if nm not in leaves:
                viol(seed, pname, "model category missing in table " + nm, round(v, 2), None)
            elif not approx(leaves[nm], v, 0.02):
                viol(seed, pname, "model!=table " + nm, round(v, 2), leaves[nm])
        for nm in leaves:
            if nm not in exp_m:
                viol(seed, pname, "table category not in model " + nm, None, leaves[nm])
        # hidden positive categories appear as Guthaben children with '+'
        for nm, v in hid_m.items():
            if not any(l.startswith(nm + " (+") for l in sn["labels"]):
                viol(seed, pname, "net-positive category missing under Guthaben " + nm, round(v, 2), None)
        # 6) cat=none
        rows, trunc = parse_list(c, "/transactions?%s&cat=none" % qs)
        if rows is not None:
            stats["none_checks"] += 1
            if any(r["split"] for r in rows):
                viol(seed, pname, "split parent listed under cat=none", 0, sum(r["split"] for r in rows))
            net, up, un = model_net(G, months=sel_months)
            tot = counted_sum(rows)
            if not approx(tot, up + un, 0.02):
                viol(seed, pname, "cat=none list != model uncategorized net", round(up + un, 2), round(tot, 2))

    def _period_months(qs):
        if qs.startswith("month="):
            return {qs[6:]}
        f_, t_ = re.search(r"from=([\d-]+)&to=([\d-]+)", qs).groups()
        return set(m.months_between(f_, t_))

    def parent_name_for(trs, tr):
        idx = trs.index(tr)
        for k in range(idx - 1, -1, -1):
            if not trs[k]["sub"]:
                return trs[k]["name"]
        return tr["name"]

    def check_jahr(seed, G, c, year):
        pname = "/jahr?y=" + year
        r = GET(c, pname)
        stats["periods"] += 1
        if r.status_code != 200:
            viol(seed, pname, "status", 200, r.status_code)
            return
        h = r.get_data(as_text=True)
        # --- category table (raw booking year, no smoothing)
        seg = h[h.index("Was kostet mich wieviel") :]
        seg = seg[: seg.index("</table>")]
        trs = [t for t in table_rows(seg) if not t["partner"]]
        exp_m, inc_m, hid_m = model_summary(
            G, year=year, use_intended=True
        )  # /jahr follows the economic month since 2026-09-11
        leaves = {}
        i = 0
        while i < len(trs):
            tr = trs[i]
            if tr["sub"]:
                i += 1
                continue
            has_sub = i + 1 < len(trs) and trs[i + 1]["sub"]
            if not has_sub:
                leaves[tr["name"]] = tr
            else:
                j = i + 1
                s = 0.0
                while j < len(trs) and trs[j]["sub"]:
                    nm = tr["name"] if trs[j]["name"] == "ohne Unterkategorie" else trs[j]["name"]
                    leaves[nm] = trs[j]
                    s += trs[j]["amount"]
                    j += 1
                stats["table_parents"] += 1
                if not approx(tr["amount"], s, 0.01 * (j - i)):
                    viol(seed, pname, "jahr parent!=children " + tr["name"], tr["amount"], round(s, 2))
            i += 1
        for nm, v in exp_m.items():
            stats["jahr_rows"] += 1
            if nm not in leaves:
                viol(seed, pname, "jahr: model category missing " + nm, round(v, 2), None)
            elif not approx(leaves[nm]["amount"], v, 0.02):
                viol(seed, pname, "jahr: model!=row " + nm, round(v, 2), leaves[nm]["amount"])
        for nm in leaves:
            if nm not in exp_m:
                viol(seed, pname, "jahr: row not in model " + nm, None, leaves[nm]["amount"])
        # row drilldowns (year=&cat=)
        for tr in trs:
            if tr["href"]:
                rows, trunc = parse_list(c, tr["href"])
                if rows is None:
                    viol(seed, pname, "jahr row drill status " + tr["name"], 200, trunc)
                    continue
                tot = -counted_sum(rows)
                if not approx(tot, tr["amount"], 0.02):
                    viol(seed, pname, "jahr row drill %s [%s]" % (tr["name"], tr["href"]), tr["amount"], round(tot, 2))
        # income table
        seg_i = h[h.index('<h2 class="mt-0">Einnahmen') :]
        seg_i = seg_i[: seg_i.index("</table>")]
        inc_rows = {t["name"]: t for t in table_rows(seg_i)}
        for nm, v in inc_m.items():
            if nm not in inc_rows:
                viol(seed, pname, "jahr: income model category missing " + nm, round(v, 2), None)
            elif not approx(inc_rows[nm]["amount"], v, 0.02):
                viol(seed, pname, "jahr: income model!=row " + nm, round(v, 2), inc_rows[nm]["amount"])
        for nm, t in inc_rows.items():
            if nm not in inc_m:
                viol(seed, pname, "jahr: income row not in model " + nm, None, t["amount"])
            elif t["href"]:
                rows, trunc = parse_list(c, t["href"])
                tot = counted_sum(rows) if rows is not None else None
                if tot is None or not approx(tot, t["amount"], 0.02):
                    viol(seed, pname, "jahr income drill %s [%s]" % (nm, t["href"]), t["amount"], tot and round(tot, 2))
        # KPI tiles on /jahr (kind= links)
        for title, sign in (("Einnahmen", 1), ("Ausgaben", -1)):
            url, kpi = tile(h, title)
            if url is None:
                continue
            rows, trunc = parse_list(c, url)
            if rows is None:
                viol(seed, pname, "jahr tile status " + title, 200, trunc)
                continue
            tot = sign * counted_sum(rows)
            if not approx(tot, kpi, 0.02):
                observations["/jahr KPI tile (%s, kind= link) != list sum" % title] += 1
        # --- matrix
        seg = h[h.index("Monat für Monat") :]
        seg = seg[: seg.index("</table>")]
        months = re.findall(r'<th class="num">(\d\d)</th>', seg)
        nm_ = len(months)
        groups = []
        for cls, hidden, body in re.findall(r'<tr(?: class="sub-(\w+)"( hidden)?)?>(.*?)</tr>', seg, re.S):
            if "<td" not in body:
                continue
            name = strip_tags(re.search(r"<td[^>]*>(.*?)</td>", body, re.S).group(1))
            cells = re.findall(r'<td class="num(?: pos)?">(.*?)</td>', body, re.S)
            vals, hrefs = [], []
            for cell in cells:
                a = re.search(r'href="([^"]+)"[^>]*>\s*(?:<b>)?([+\-]?\d+)', cell, re.S)
                if a:
                    txt = a.group(2)
                    vals.append(-float(txt[1:]) if txt.startswith("+") else float(txt))
                    hrefs.append(htmlmod.unescape(a.group(1)))
                else:
                    vals.append(0.0)
                    hrefs.append(None)
            if len(vals) != nm_ + 1:
                viol(seed, pname, "matrix row cell count " + name, nm_ + 1, len(vals))
                continue
            groups.append({"name": name, "sub": bool(cls), "vals": vals, "hrefs": hrefs})
        i = 0
        while i < len(groups):
            g = groups[i]
            stats["matrix_rows"] += 1
            kids = []
            j = i + 1
            while j < len(groups) and groups[j]["sub"]:
                kids.append(groups[j])
                j += 1
            if kids:
                stats["table_parents"] += 1
                for k in range(nm_ + 1):
                    s = sum(kid["vals"][k] for kid in kids)
                    if not approx(g["vals"][k], s, 0.5 * len(kids) + 0.01):
                        viol(
                            seed,
                            pname,
                            "matrix parent!=children %s col %s" % (g["name"], (months + ["Σ"])[k]),
                            round(s, 1),
                            g["vals"][k],
                        )
            # sigma == sum of month cells (rounded display)
            s = sum(g["vals"][:nm_])
            if not approx(g["vals"][nm_], s, 0.5 * nm_ + 0.01):
                viol(seed, pname, "matrix Σ != sum(months) " + g["name"], round(s, 1), g["vals"][nm_])
            # each cell link
            for k in range(nm_ + 1):
                href = g["hrefs"][k]
                if not href:
                    continue
                stats["matrix_cells"] += 1
                rows, trunc = parse_list(c, href)
                if rows is None:
                    viol(seed, pname, "matrix cell status " + href, 200, trunc)
                    continue
                tot = -counted_sum(rows)
                if not approx(tot, g["vals"][k], 0.51):
                    viol(
                        seed,
                        pname,
                        "matrix cell drill %s %s [%s]" % (g["name"], (months + ["Σ"])[k], href.split("?")[1]),
                        g["vals"][k],
                        round(tot, 2),
                    )
            for kid in kids:
                for k in range(nm_ + 1):
                    href = kid["hrefs"][k]
                    if not href:
                        continue
                    stats["matrix_cells"] += 1
                    rows, trunc = parse_list(c, href)
                    if rows is None:
                        viol(seed, pname, "matrix cell status " + href, 200, trunc)
                        continue
                    tot = -counted_sum(rows)
                    if not approx(tot, kid["vals"][k], 0.51):
                        viol(
                            seed,
                            pname,
                            "matrix child cell drill %s/%s %s [%s]"
                            % (g["name"], kid["name"], (months + ["Σ"])[k], href.split("?")[1]),
                            kid["vals"][k],
                            round(tot, 2),
                        )
            i = j
        # matrix sum column vs category table (same page, same year)
        sig = {}
        i = 0
        while i < len(groups):
            g = groups[i]
            kids = []
            j = i + 1
            while j < len(groups) and groups[j]["sub"]:
                kids.append(groups[j])
                j += 1
            if kids:
                for kid in kids:
                    nm = g["name"] if kid["name"] == "ohne Unterkategorie" else kid["name"]
                    sig[nm] = kid["vals"][nm_]
            else:
                sig[g["name"]] = g["vals"][nm_]
            i = j
        for nm, tr in leaves.items():
            if nm in sig and not approx(sig[nm], tr["amount"], 0.51):
                viol(seed, pname, "matrix Σ != 'Was kostet' row " + nm, tr["amount"], sig[nm])
        # model with smoothing (intended months) vs matrix cells
        net_m, up, un = model_net(G, year=year, use_intended=True)
        per_month = defaultdict(lambda: defaultdict(float))
        for r in G["rows"]:
            if r["status"] != "active" or r["month"][:4] != year:
                continue
            if r["splits"]:
                for c_, a_ in r["splits"]:
                    if G["kind_of"][c_] != "transfer":
                        per_month[G["name_of"][c_]][r["month"][5:7]] += -a_
            else:
                v = r["eff"] if r["eff"] is not None else r["amount"]
                if r["cat"] is None:
                    per_month["Unkategorisiert"][r["month"][5:7]] += -v
                elif G["kind_of"][r["cat"]] == "expense":
                    per_month[G["name_of"][r["cat"]]][r["month"][5:7]] += -v
        leafrows = {}
        i = 0
        while i < len(groups):
            g = groups[i]
            kids = []
            j = i + 1
            while j < len(groups) and groups[j]["sub"]:
                kids.append(groups[j])
                j += 1
            if kids:
                for kid in kids:
                    leafrows[g["name"] if kid["name"] == "ohne Unterkategorie" else kid["name"]] = kid
            else:
                leafrows[g["name"]] = g
            i = j
        for nm, cols in per_month.items():
            for mm, v in cols.items():
                if abs(v) < 0.005:
                    continue
                if mm not in months:
                    viol(seed, pname, "matrix month column missing %s (%s)" % (mm, nm), round(v, 2), None)
                    continue
                k = months.index(mm)
                got = leafrows[nm]["vals"][k] if nm in leafrows else None
                if got is None or not approx(got, v, 0.51):
                    viol(seed, pname, "matrix cell != model %s %s" % (nm, mm), round(v, 2), got)

    def check_adversarial(seed, c):
        for u in (
            "/?from=2020-01&to=2099-12",
            "/?range=last0",
            "/?range=-1",
            "/?month=abc",
            "/transactions?cat=abc",
            "/transactions?cat=1,,2",
            "/transactions?cat=999999",
            "/?range=abc",
            "/?from=abc&to=xyz",
            "/transactions?from=2020-01&to=2099-12",
            "/transactions?month=abc",
            "/transactions?cat=none,none",
            "/transactions?cat=,",
            "/jahr?y=abc",
            "/jahr?y=1999",
            "/?range=last999",
        ):
            r = GET(c, u)
            stats["adversarial"] += 1
            if r.status_code != 200:
                viol(seed, "adversarial", u, 200, r.status_code)
        # last0 semantics
        h = GET(c, "/?range=last0").get_data(as_text=True)
        mo = re.search(r"(\d+) Monate</span>", h)
        if mo and int(mo.group(1)) > 1:
            observations["?range=last0 shows ALL months (%s), not 0/1" % mo.group(1)] += 1

    # --------------------------------------------------------------- main
    for seed in SEEDS:
        G = gen(seed)
        c = m.app.test_client()
        rng = random.Random(seed * 7919)
        pop = G["populated"]
        years = sorted({mm[:4] for mm in pop})
        periods = [
            ("/?month=%s" % rng.choice(pop), "month"),
            ("/?range=%s" % rng.choice(years), "range=year"),
            ("/?range=all", "range=all"),
            ("/?range=last3", "range=last3"),
        ]
        # year boundary from/to
        ys = [y for y in years if str(int(y) + 1) in years]
        if ys:
            y = rng.choice(ys)
            periods.append(("/?from=%s-11&to=%s-02" % (y, int(y) + 1), "from/to boundary"))
        if VERBOSE:
            print(
                "seed %d: %d rows, months %s..%s (%d populated), fixed %s..%s, probe=%s posuncat=%s"
                % (
                    seed,
                    len(G["rows"]),
                    pop[0],
                    pop[-1],
                    len(pop),
                    G["fx_months"][0],
                    G["fx_months"][-1],
                    G["with_probe"],
                    G["with_pos_uncat"],
                )
            )
        for url, pn in periods:
            try:
                check_dashboard(seed, G, c, url, pn + " " + url)
            except Exception as e:
                viol(seed, pn + " " + url, "EXCEPTION in dashboard check", "", repr(e))
                if VERBOSE:
                    import traceback

                    traceback.print_exc()
        for y in years:
            try:
                check_jahr(seed, G, c, y)
            except Exception as e:
                viol(seed, "/jahr?y=" + y, "EXCEPTION in jahr check", "", repr(e))
                if VERBOSE:
                    import traceback

                    traceback.print_exc()
        check_adversarial(seed, c)
        stats["seeds"] += 1

    print("STATS", dict(stats))
    print("OBSERVATIONS")
    for k, v in sorted(observations.items()):
        print("  %5d  %s" % (v, k))
    print("VIOLATIONS: %d" % len(violations))
    # group by 'what' prefix
    grp = defaultdict(list)
    for s, p, w, e, a in violations:
        key = re.sub(r"\[.*?\]", "", w)
        key = re.sub(r"\(link=.*?\)", "", key)
        grp[key.split(" ")[0] + " " + " ".join(key.split(" ")[1:3])].append((s, p, w, e, a))
    for k, lst in sorted(grp.items(), key=lambda kv: -len(kv[1])):
        print("== %s  (%d)" % (k, len(lst)))
        for s, p, w, e, a in lst[:6]:
            print("   seed=%s %s :: %s  expected=%s actual=%s" % (s, p, w, e, a))
    check("fuzz: no violations across all seeds", not violations, len(violations))
    finish()
