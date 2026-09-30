cd "$HOME/Desktop/missing-projects"
python3 compare_wrike_raw_rows.py --snapshot snowflake_snapshot.csv --akash output/local_validation/20260930T213320_958948Z/wrike_local_full.csv --output output/raw_effort_review_20260930
