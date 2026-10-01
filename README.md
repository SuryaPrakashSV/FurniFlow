cd ~/Desktop/wrike_incremental_step1

find output -type f \( \
  -name 'summary.json' -o \
  -name 'snapshot.json' -o \
  -name 'tasks_*.csv' -o \
  -name 'project_history.sqlite' \
\)
