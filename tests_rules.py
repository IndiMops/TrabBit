from pathlib import Path
from trabbit.rules import RulesEngine

rules = RulesEngine(Path("config/rules.json"))
category, tags = rules.classify(
    title="Вісімдесят шість (Сезон 1, серії 1-4 з 23) BDRip 1080p H.265 Ukr/Jap | Sub Ukr",
    torrent_name="86 Eighty Six [BDRip 1080p HEVC]",
    watched=True,
    priority="high",
)
assert category == "Anime"
assert "1080p" in tags
assert "bdrip" in tags
assert "hevc" in tags
assert "watchlist" in tags
assert "priority-high" in tags
assert "ongoing" in tags
print("rules: OK")
