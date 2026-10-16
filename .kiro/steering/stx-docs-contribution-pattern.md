---
inclusion: manual
---

# StarlingX Docs — Dev-Authored Doc-Change Pattern (rulebook)

How a **developer** contributes a documentation change to `starlingx/docs`. The dev AUTHORS
the `.rst`; the **doc-team writer reviews, +2/W+1, and merges**. Follow this verbatim so the
output reads idiomatic, not invented.

**Canonical reference (study it):** Jira **CGTS-105382** "Doc: Change gnss_uart_disable default"
→ OpenDev **starlingx/docs** change **1003094** (MERGED, `review.opendev.org/c/starlingx/docs/+/1003094`).
All concrete facts below are extracted from that change.

---

## 1. Repository layout — WHERE docs live & how a page is wired

Three files touched for a new topic (mirror this exactly):

| What | Path (from change 1003094) |
|------|----------------------------|
| **New topic page** | `doc/source/system_configuration/kubernetes/<topic-slug>.rst` |
| **Toctree (register the page)** | `doc/source/system_configuration/kubernetes/index-sysconf-kub-78f0e1e9ca5a.rst` |
| **Abbreviations (only if a NEW acronym is used)** | `doc/source/shared/abbrevs.txt` |

Rules:
- **New page filename** = kebab-case slug, no hash suffix when the dev names it
  (e.g. `ptp-gnss-uart-disable-default-change.rst`). Hash-suffixed names you see in the
  toctree (`...-484051f0`) are writer-generated — do NOT invent one.
- **Toctree wiring:** add the page's filename (NO `.rst` extension) as a single new line in the
  **correct section's `.. toctree::`** block of the `index-sysconf-kub-*.rst`. In 1003094 the
  line `ptp-gnss-uart-disable-default-change` was slotted inside the **`PTP Server Configuration`**
  section (`:maxdepth: 3`), among its sibling PTP topics. Place a new topic under the section it
  belongs to; keep ordering sensible.
- **Abbrevs:** add `.. |XXX| replace:: :abbr:`XXX (Full Expansion)`` **in alphabetical position**,
  singular and plural on **separate** lines. In 1003094 only `|UART|` was new (inserted between the
  `TX` and `UDP` entries); `|PTP|`, `|GNSS|`, `|NIC|/|NICs|`, `|NMEA|` already existed — reuse
  existing substitutions, never redefine them. If no new acronym, do NOT touch abbrevs.txt.

---

## 2. The Jira side

- **Summary prefix:** `Doc: <imperative short description>` (e.g. `Doc: Change gnss_uart_disable default`).
- **Component:** `Docs`.
- **Reporter = Assignee = the DEV** (CGTS-105382: Zelak both).
- **Type:** typically `Bug` (doc gap/change); fix-version set (e.g. `26.10-WRCP-GA`); priority as warranted (P2).
- **Labels:** carry the originating program/customer label (CGTS-105382: `Samsung`).
- **Link the software Jira(s)** the doc describes (CGTS-105382 references CGTS-99420 and CGTS-91942).
- The dev posts the Gerrit link as a Jira comment when ready for review; card is resolved **after merge**.

---

## 3. The contribution flow (who does what — the dev merges NOTHING)

1. **Dev authors** the `.rst` + toctree line (+ abbrevs if needed) and pushes to Gerrit
   (`git review`). Dev owns every patchset.
2. **Zuul** runs `openstack-tox-linters` + `openstack-tox-docs`, votes **Verified+1** on check.
   These MUST pass. A doc build failure blocks merge.
3. **Peer/SME review** (feature/network team) gives **Code-Review+1** (in 1003094: Cole Walker,
   Tara Subedi). This is informal "technically correct".
4. **Doc-team writer** does the authoritative review, applies style edits (may push an edit as a
   new patchset), then votes **Code-Review+2 AND Workflow+1** — this is who **merges**.
   In 1003094 that writer was **Juanita-Balaraj (jbalaraj)** (and she is the Gerrit `Commit` author).
5. **Lab validation gate:** the doc merges **only after the feature is lab-validated**. In 1003094
   the PV tester (**Tridal Patel**) was added to the review to confirm testing of the software Jira
   (CGTS-99420) **before** W+1. Do not merge doc ahead of feature validation.
6. **Zuul gate** → **Verified+2** → **merged by Zuul** (owner: Zuul service user), then promote
   publishes the docs site.

> The DEV never self-merges. The dev's job ends at "patch up, CI green, review addressed."
> +2/W+1/merge belong to the doc team.

---

## 4. `.rst` conventions actually used in 1003094 (write idiomatic)

- **Anchor + title:** blank line, then `.. _<page-slug>:`, blank line, then an **overline+underline**
  title with `=` (both lines, length ≥ title):
  ```rst
  .. _ptp-gnss-uart-disable-default-change:

  ===============================================
  Update ts2phc gnss_uart_disable Default Setting
  ===============================================
  ```
- **Local contents** right after the intro sentence:
  ```rst
  .. contents::
     :local:
     :depth: 1
  ```
- **Section headers** underlined with `-` (match length): `Overview`, `Re-enable ...`,
  `Restore Default Behavior`.
- **Abbreviation substitutions** inline for every defined acronym: `|PTP|`, `|GNSS|`, `|UART|`,
  `|NIC|`, `|NMEA|`, and the product macro **`|prod|`** (never hard-code the product name).
- **Literals / parameters / values** in double backticks: ``` ``ts2phc`` ```, ``` ``gnss_uart_disable`` ```,
  ``` ``true`` ```, ``` ``false`` ```.
- **Admonitions:** `.. note::` for behavioral-change / caveats (indented body, blank line after).
- **Procedures:** `.. rubric:: |proc|` then numbered steps using `#.` (two trailing spaces →
  `#.  `), each command in:
  ```rst
  .. code-block:: none

      ~(keystone_admin)$ system ptp-instance-parameter-add <name> gnss_uart_disable=true
  ```
  Use the `~(keystone_admin)$` prompt and `<angle-bracket>` placeholders with an explicit
  "Replace `<...>` with ..." sentence.
- **Style/length:** keep prose lines short (~≤ 79 cols — `tox-linters` enforces); one sentence
  per idea; blank line between every block/directive; file ends with a trailing newline.

## 5. Commit message conventions

- Imperative subject ≤ ~50 chars: `Document ts2phc gnss_uart_disable default change`.
- Body (wrap ~72): explain the behavioral change AND state the toctree registration.
- Trailers: `Assisted-by:` (if an agent helped), **`Signed-off-by:`** (required),
  **`Change-Id:`** (gerrit hook). One topic/branch (`topic: gnss-uart`, branch `master`).

---

## 6. Pitfalls the doc agent MUST NOT get wrong

- Do **not** forget the toctree line — an orphan `.rst` fails the docs build and will not render.
- Do **not** invent a hash suffix on the new page filename; use a clean kebab-case slug.
- Do **not** redefine an abbreviation that already exists in `abbrevs.txt`; only add genuinely new
  ones, alphabetically, singular+plural separately.
- Do **not** hard-code the product name — use `|prod|`.
- Do **not** self-merge or assume merge authority; produce a review-ready patch only.
- Do **not** merge the doc before the feature is lab-validated (PV sign-off gates the merge).
- Keep lines short and directives blank-line-separated, or `tox-docs`/`tox-linters` will fail CI.
