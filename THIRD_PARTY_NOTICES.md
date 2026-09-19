# Third-party notices

`glace-catalog` includes third-party code that is distributed under licenses
different from its own (MIT, see [`LICENSE`](LICENSE)). The files named below
stay under the license given for them here. In the event that a required notice
is missing, please [raise an issue](https://github.com/lqgentner/glace-catalog/issues).

## License notice for portolan-catalog-template

This repository was scaffolded from `portolan-sdi/portolan-catalog-template` and
still carries code from it. `tests/test_conformance.py`, `tests/test_links.py`
and `tests/test_stac_valid.py` are kept close to upstream on purpose, so that a
fix from the template can be merged; `tests/run_all.py` and
`.github/workflows/ci.yml` are derived from their upstream counterparts and
substantially rewritten. All five have been modified, and each marks its own
deviations where they occur.

Copyright the portolan-catalog-template authors
Licensed under the Apache License, Version 2.0

A copy of the license is in [`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt).

Available at
https://github.com/portolan-sdi/portolan-catalog-template/blob/main/LICENSE

## License notice for rio-pmtiles

`src/glace_catalog/tiler.py` is adapted from rio-pmtiles 1.2.1
(`rio_pmtiles/worker.py` and `rio_pmtiles/scripts/cli.py`), which is itself
derived from rio-mbtiles by Sean Gillies at Mapbox.

```text
The MIT License (MIT)

Copyright © 2025 Protomaps LLC, 2014-2021 Mapbox

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Available at
https://github.com/protomaps/PMTiles/blob/main/python/rio-pmtiles/LICENSE
