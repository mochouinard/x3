# Contributing to x3

x3 is a personal research project, and contributions are welcome: bug
reports, fixes, and improvements alike.

## The license, in short

x3 is free for personal and noncommercial use under the PolyForm Strict
License 1.0.0 (see `LICENSE`). That license on its own does not allow changes.
The additional permission in `LICENSE` lets you change x3 for one purpose: to
prepare, test and send those changes back as a pull request to the official
repository. A changed copy is not for other uses or for sharing anywhere else.

## Contribution terms

By submitting a contribution (a pull request, a patch, or code in an issue),
you agree that:

1. it is your own work, or you have the right to submit it;
2. you grant Marc O. Chouinard a perpetual, worldwide, non-exclusive,
   royalty-free, irrevocable license to use, copy, modify, sublicense and
   distribute your contribution, as part of x3 or otherwise, under any terms,
   including the license x3 is released under at the time;
3. you are not expected to provide support for it, and it comes as is.

If you can't agree to these terms, please open an issue describing the change
instead of sending code.

## Before you send a pull request

- Run the tests: `python tests/run.py` (see the README for the router plugin
  set some of them need).
- Keep each pull request to one change, and say why in its description.
- **Never attach captures, address books or audit files.** The Winbox
  protocol carries passwords inside its encrypted session, and `Addresses.cdb`,
  session logs and `.wba` audit files hold them in plain text. Redact or
  rebuild any example from a lab device.
- Test against a lab router, not a production one.

## Adding or improving a language

Each language is one file, `x3/ui/i18n/<code>.json`, mapping the English text
to its own. `tools/i18n-catalog.json` lists every string x3 can show, most
seen first.

```
python tools/i18n_catalog.py --new de --name Deutsch   # start de.json with every string
python tools/i18n_catalog.py --update fr               # add what fr.json does not list yet
python tests/run.py i18n                               # check the files
x3 --lang de                                           # try it
```

- Leave a value empty (`""`) to keep the English: acronyms, product names, and
  labels that read the same.
- Keep what the English has: the `&` before a menu letter (move it to a letter
  of your word), HTML tags, `{placeholders}`, line breaks, and a trailing `…`.
- `"colon"` at the top of the file is how your language ends a label: French
  uses `" :"`.
- Running the tool with a router's plugin sets on disk (a `x3ctl mirror`
  folder, or the official client's cache) adds the labels of those firmware
  versions to the catalog. It only ever adds.

## Reporting a bug

Say what you did, what you expected, and what happened, with the x3 version
(Help ▸ About), your operating system and the router's firmware version. The
application log (`x3.log`, in the state folder the README lists for your
platform) usually shows what went wrong. Check it for anything private before
attaching it.
