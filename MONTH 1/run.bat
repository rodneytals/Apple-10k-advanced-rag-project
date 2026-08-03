@echo off
echo ================================================
echo   Advanced RAG Project - MONTH 1 CAPSTONE
echo ================================================

call conda activate rag-Ai

echo.
echo Using Python from conda environment:
"C:\Users\rodne\miniconda3\envs\rag-Ai\python.exe" --version

echo.
echo Setting PYTHONPATH...
set PYTHONPATH=%PYTHONPATH%;%CD%

echo.
echo Starting RAG system...
echo.

"C:\Users\rodne\miniconda3\envs\rag-Ai\python.exe" data\main.py

echo.
echo ================================================
echo Script finished.
echo Press any key to close this window...
pause