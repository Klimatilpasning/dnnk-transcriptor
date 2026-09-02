from transcribe import CATEGORIES, scrape_category_for_videos, load_state

state = load_state()
known_ids = set(state.keys())

all_found = set()
for cat_name, cat_url in CATEGORIES.items():
    print(f"Scraping category: {cat_name}")
    try:
        videos = scrape_category_for_videos(cat_url)
    except Exception as e:
        print(f"  ERROR scraping {cat_name}: {e}")
        continue
    all_found.update(videos)

new_ids = sorted(all_found - known_ids)
print(f"\nTotal found: {len(all_found)}, known: {len(known_ids)}, new: {len(new_ids)}")
for vid in new_ids:
    print("NEW:", vid)
