@echo off
python "%~dp0scripts\replace_values_in_files.py" --mapping "%~dp0replace_mapping.csv" --valid-users-file "%~dp0existing_user.csv" --default-user-id 005S900000dr9BaIAI --path "%~dp0state\flat_file_drop\Case"
