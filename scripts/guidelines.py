"""Build or inspect the local document library without model/API calls."""

import argparse
import json

from bpcagent.tools.guideline_library import GuidelineLibrary, build_library


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/guidelines.jsonc")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="Publish a new snapshot; retain previous snapshots")
    commands.add_parser("catalog")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--document-id")
    search.add_argument("--version")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--include-updates", action="store_true")
    section = commands.add_parser("section")
    section.add_argument("document_id")
    section.add_argument("section_id")
    section.add_argument("--offset", type=int, default=0)
    section.add_argument("--max-chars", type=int, default=12000)
    page = commands.add_parser("page")
    page.add_argument("document_id")
    page.add_argument("pdf_page", type=int)
    page.add_argument("--output", required=True)
    args = parser.parse_args()
    library = GuidelineLibrary(args.config, external=False)
    if args.command == "build":
        result = build_library(args.config)
    elif args.command == "catalog":
        result = library.catalog()
    elif args.command == "search":
        result = library.search(args.query, args.document_id, args.version, args.limit, args.include_updates)
    elif args.command == "section":
        result = library.section(args.document_id, args.section_id, args.offset, args.max_chars)
    else:
        result = library.page(args.document_id, args.pdf_page, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
