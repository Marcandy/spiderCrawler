# Source access record

Every host the project fetches from, and why it is allowed. Re-check before changing sources.
The fetcher also re-checks robots.txt on every run (redirect-aware, see PLAN.md section 3).

## In use

### www.pacourts.us (Pennsylvania Supreme Court rules committee pages)

| Date | Check | Result |
|---|---|---|
| 2026-11-01 | `GET /robots.txt` | 404, no robots.txt. Allowed by convention. |
| 2026-11-01 | Terms of use | No terms page found (`/terms-of-use` is 404). Only a translation disclaimer is linked from the homepage. |

Court rules are government edicts and not copyrightable. Committee reports are quoted briefly
with a link to the source.

Pages: the 8 committee pages listed in `sources.yaml`. Linked PDFs under `/Storage/media/pdfs/`.

## Not used

### www.pacodeandbulletin.gov (Pennsylvania Code and Pennsylvania Bulletin)

| Date | Check | Result |
|---|---|---|
| 2026-11-01 | `GET /robots.txt` | Ends with `User-agent: *` / `Disallow: /`. Googlebot is also barred from `/Display/pacode` and `/Display/pabull`. |

**Not allowed.** Never fetched. Asking the Legislative Reference Bureau for permission is the
option to revisit if pacourts.us proves too thin.

### www.pabulletin.com

| Date | Check | Result |
|---|---|---|
| 2026-11-01 | `GET /robots.txt` | 302 to `https://www.pacodeandbulletin.gov` (the homepage, not its robots.txt). |

**Not allowed**: it is an alias of the host above. A naive `urllib.robotparser` check reports
"allowed" here because it parses the homepage HTML as robots rules and finds none. The fetcher
evaluates robots.txt on the final host after redirects and treats an HTML or unreachable
robots.txt as disallowed, so this alias is refused.
