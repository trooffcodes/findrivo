
from ddgs import DDGS

queries = [
    "coffee",
    "Pune",
    "India",
    "coffee Pune",
]

for query in queries:
    print(f"\n{'=' * 40}\nQuery: {query}", flush=True)

    for backend in ["duckduckgo", "auto"]:
        try:
            results = DDGS(timeout=15).text(
                query,
                max_results=5,
                backend=backend,
            )

            print(
                f"Backend={backend}: {len(results)} results",
                flush=True,
            )

            for result in results[:2]:
                print(
                    f"  {result.get('title')} | "
                    f"{result.get('href')}",
                    flush=True,
                )

        except Exception as exc:
            print(
                f"Backend={backend}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
