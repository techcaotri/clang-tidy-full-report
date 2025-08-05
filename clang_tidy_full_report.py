#!/usr/bin/env python3
"""
Comprehensive Clang-Tidy Report Generator with Progress Tracking and Output Options
Generates reports in multiple formats with optimized HTML for large projects
"""

import subprocess
import json
import csv
import sys
import re
import os
import time
import threading
import fnmatch
import tempfile
import multiprocessing
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import argparse
import html
import urllib.parse

# Try to import tqdm for better progress bars
try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False
    print("Note: Install 'tqdm' for better progress bars: pip install tqdm")

class PrintMode:
    """Print mode constants"""
    QUIET = 'quiet'
    PROGRESS = 'progress'
    VERBOSE = 'verbose'
    FULL = 'full'

class ProgressBar:
    """Simple progress bar implementation for when tqdm is not available"""
    def __init__(self, total, desc="", width=50):
        self.total = total
        self.desc = desc
        self.width = width
        self.current = 0
        self.start_time = time.time()
        
    def update(self, n=1):
        self.current += n
        self._display()
        
    def _display(self):
        if self.total == 0:
            return
            
        percentage = self.current / self.total
        filled = int(self.width * percentage)
        bar = '█' * filled + '░' * (self.width - filled)
        elapsed = time.time() - self.start_time
        
        if self.current > 0:
            eta = (elapsed / self.current) * (self.total - self.current)
            eta_str = f"ETA: {int(eta)}s"
        else:
            eta_str = "ETA: --"
            
        sys.stdout.write(f'\r{self.desc}: |{bar}| {self.current}/{self.total} ({percentage:.1%}) {eta_str}')
        sys.stdout.flush()
        
    def close(self):
        sys.stdout.write('\n')
        sys.stdout.flush()

class ClangTidyReporter:
    def __init__(self, build_dir, print_mode=PrintMode.PROGRESS, output_dir=None, exclude_patterns=None, debug_exclude=False, header_filter=None, project_dir=None, debug_parsing=False, save_raw_output=False):
        self.build_dir = build_dir
        self.warnings = []
        self.warnings_set = set()  # Track unique warnings to avoid duplicates
        self.print_mode = print_mode
        self.output_dir = output_dir or '.'
        self.exclude_patterns = exclude_patterns or []
        self.debug_exclude = debug_exclude
        self.debug_parsing = debug_parsing  # New: debug parsing flag
        self.save_raw_output = save_raw_output  # New: save raw output flag
        self.header_filter = header_filter  # New: header filter pattern
        self.project_dir = os.path.abspath(project_dir) if project_dir else None  # New: project directory
        self.files_to_check = []  # Initialize before loading
        self.current_file = ""
        self.files_processed = 0
        self.file_warnings = defaultdict(int)  # Track warnings per file
        self.checks_used = None  # Track which checks were used
        self.config_file_used = None  # Track if config file was used
        self.raw_outputs = []  # Store raw outputs for debugging
        self.last_command = None  # Store last clang-tidy command
        self.compile_commands = self._load_compile_commands()  # This will populate files_to_check
        
        # Create output directory if it doesn't exist
        if self.output_dir != '.':
            os.makedirs(self.output_dir, exist_ok=True)
            print(f"Output directory: {os.path.abspath(self.output_dir)}")
        
        if self.project_dir:
            print(f"Project directory: {self.project_dir}")
        
        if self.debug_parsing:
            print("Debug parsing: ENABLED - will show detailed parsing information")
        
        if self.save_raw_output:
            print("Raw output saving: ENABLED - will save intermediate results")
        
    def _get_display_path(self, file_path):
        """Get display path - relative to project dir if specified, otherwise try relative to cwd"""
        if self.project_dir:
            try:
                abs_path = os.path.abspath(file_path)
                if abs_path.startswith(self.project_dir):
                    return os.path.relpath(abs_path, self.project_dir)
            except:
                pass
        
        # Fall back to relative path from cwd
        try:
            return os.path.relpath(file_path)
        except:
            return file_path
    
    def _print_stage(self, stage, status="STARTED"):
        """Print a stage marker"""
        if self.print_mode == PrintMode.QUIET:
            return
            
        timestamp = datetime.now().strftime("%H:%M:%S")
        if status == "STARTED":
            print(f"\n[{timestamp}] ▶ {stage}")
        elif status == "COMPLETED":
            print(f"[{timestamp}] ✓ {stage}")
        elif status == "FAILED":
            print(f"[{timestamp}] ✗ {stage}")
    
    def _get_output_path(self, filename):
        """Get the full path for an output file"""
        return os.path.join(self.output_dir, filename)
    
    def _should_exclude(self, file_path):
        """Check if file should be excluded based on patterns"""
        # Normalize path for comparison
        normalized_path = os.path.normpath(file_path).replace('\\', '/')
        
        for pattern in self.exclude_patterns:
            # Normalize pattern
            pattern = pattern.replace('\\', '/')
            
            # Handle ** glob pattern
            if '**' in pattern:
                # Convert ** to a more flexible pattern
                # For "external/**", we want to match any path containing "/external/" or starting with "external/"
                if pattern.startswith('**/'):
                    # Pattern like "**/test" - match anywhere
                    search_pattern = pattern[3:]  # Remove **/
                    if '**' in search_pattern:
                        # Handle nested ** patterns
                        regex_pattern = search_pattern.replace('**', '.*').replace('*', '[^/]*')
                    else:
                        regex_pattern = search_pattern.replace('*', '[^/]*')
                    
                    # Check if pattern appears anywhere in the path
                    if re.search(regex_pattern, normalized_path):
                        return True
                elif pattern.endswith('/**'):
                    # Pattern like "external/**" - match directory and everything under it
                    dir_pattern = pattern[:-3]  # Remove /**
                    # Check if path contains this directory
                    # Match /external/ or starts with external/
                    if f"/{dir_pattern}/" in normalized_path or normalized_path.startswith(f"{dir_pattern}/"):
                        return True
                else:
                    # Pattern with ** in the middle like "src/**/test"
                    regex_pattern = pattern.replace('**', '.*').replace('*', '[^/]*')
                    # Allow pattern to match anywhere in the path
                    if re.search(regex_pattern, normalized_path):
                        return True
            else:
                # Use standard glob matching
                if fnmatch.fnmatch(normalized_path, pattern):
                    return True
                
                # For patterns like "*.test.cpp", check just the filename
                if '*' in pattern and '/' not in pattern:
                    filename = os.path.basename(normalized_path)
                    if fnmatch.fnmatch(filename, pattern):
                        return True
        
        return False
            
    def _load_compile_commands(self):
        """Load compile_commands.json"""
        self._print_stage("Loading compile_commands.json")
        compile_commands_path = Path(self.build_dir) / "compile_commands.json"
        if not compile_commands_path.exists():
            self._print_stage("Loading compile_commands.json", "FAILED")
            print(f"Error: {compile_commands_path} not found!")
            sys.exit(1)
            
        with open(compile_commands_path) as f:
            commands = json.load(f)
        
        # Extract and normalize file paths
        all_files = []
        excluded_files = []
        excluded_by_pattern = defaultdict(list)  # Track which pattern excluded which files
        
        # Show debug header for exclude debugging
        if self.debug_exclude and self.exclude_patterns:
            print("\n  DEBUG: Pattern matching details (showing first few files):")
            print(f"  Patterns to match: {self.exclude_patterns}")
            print("  " + "-" * 60)
        
        debug_count = 0
        debug_exclude_active = self.debug_exclude  # Local variable to control debug output
        
        for cmd in commands:
            file_path = cmd['file']
            # Handle relative paths - resolve them relative to the directory in compile_commands
            if 'directory' in cmd and not os.path.isabs(file_path):
                file_path = os.path.join(cmd['directory'], file_path)
            # Normalize the path
            file_path = os.path.normpath(file_path)
            
            # Temporarily control debug output
            self.debug_exclude = debug_exclude_active and len(all_files) < 20
            
            # Check exclusion patterns
            excluded = False
            matched_pattern = None
            for pattern in self.exclude_patterns:
                if self._should_exclude_with_pattern(file_path, pattern):
                    excluded_files.append(file_path)
                    excluded_by_pattern[pattern].append(file_path)
                    excluded = True
                    matched_pattern = pattern
                    break
            
            if not excluded:
                self.files_to_check.append(file_path)
                # Show first few files that will be processed in debug mode
                if debug_exclude_active and debug_count < 5:
                    debug_count += 1
                    print(f"    ✓ Will process: {self._get_display_path(file_path)}")
            elif debug_exclude_active and len(excluded_files) <= 5:
                # Show first few excluded files with the pattern that matched
                print(f"    ✗ Excluded by '{matched_pattern}': {self._get_display_path(file_path)}")
            
            all_files.append(file_path)
            
            # Limit debug output
            if debug_exclude_active and len(all_files) == 20:
                print("  " + "-" * 60)
                print("  (Debug output limited to first 20 files)")
                print(f"  So far: {len(self.files_to_check)} included, {len(excluded_files)} excluded")
        
        # Restore original debug_exclude value
        self.debug_exclude = debug_exclude_active
            
        self._print_stage(f"Loading compile_commands.json - Found {len(all_files)} files", "COMPLETED")
        
        if excluded_files:
            print(f"  Excluded {len(excluded_files)} files based on exclusion patterns")
            
            # Always show exclusion summary by pattern (not just in verbose mode)
            if self.exclude_patterns:
                print("\n  Exclusion summary by pattern:")
                for pattern in self.exclude_patterns:
                    count = len(excluded_by_pattern.get(pattern, []))
                    if count > 0:
                        print(f"    '{pattern}': {count} files")
                        # Show a few examples of excluded files for this pattern
                        if self.print_mode in [PrintMode.VERBOSE, PrintMode.FULL]:
                            examples = excluded_by_pattern[pattern][:3]
                            for example in examples:
                                # Show project-relative path if possible
                                display_path = self._get_display_path(example)
                                print(f"      e.g., {display_path}")
                            if len(excluded_by_pattern[pattern]) > 3:
                                print(f"      ... and {len(excluded_by_pattern[pattern]) - 3} more")
            
            # Show detailed excluded files in verbose mode (directory grouping)
            if self.print_mode in [PrintMode.VERBOSE, PrintMode.FULL] and len(excluded_files) > 10:
                print("\n  Excluded directories (grouped):")
                # Group by directory for better readability
                excluded_dirs = defaultdict(list)
                for file_path in excluded_files:
                    dir_path = os.path.dirname(file_path)
                    excluded_dirs[dir_path].append(os.path.basename(file_path))
                
                # Sort directories by path to group related directories together
                sorted_dirs = sorted(excluded_dirs.items(), key=lambda x: x[0])
                
                # Show directories with proper grouping
                shown_dirs = 0
                total_dirs = len(excluded_dirs)
                current_parent = None
                
                for dir_path, files in sorted_dirs[:20]:  # Show up to 20 directories
                    shown_dirs += 1
                    
                    # Check if this is a subdirectory of the previous one
                    if current_parent and dir_path.startswith(current_parent + os.sep):
                        # This is a subdirectory, indent it
                        indent = "      "
                    else:
                        # New top-level directory
                        indent = "    "
                        current_parent = dir_path
                    
                    # Show directory with file count
                    display_path = self._get_display_path(dir_path)
                    print(f"{indent}- {display_path}/ ({len(files)} files)")
                
                if total_dirs > 20:
                    print(f"    ... and {total_dirs - 20} more directories")
        
        print(f"  Will analyze {len(self.files_to_check)} files")
        
        # Debug: print first few files if verbose
        if self.print_mode in [PrintMode.VERBOSE, PrintMode.FULL] and self.files_to_check:
            print(f"\n  First file to analyze: {self._get_display_path(self.files_to_check[0])}")
            if len(self.files_to_check) > 1:
                print(f"  Last file to analyze: {self._get_display_path(self.files_to_check[-1])}")
        
        return commands
    
    def _should_exclude_with_pattern(self, file_path, pattern):
        """Check if file should be excluded by a specific pattern"""
        # Normalize path for comparison - ensure forward slashes
        normalized_path = os.path.normpath(file_path).replace('\\', '/')
        # Remove any leading ./ 
        if normalized_path.startswith('./'):
            normalized_path = normalized_path[2:]
        # Remove any duplicate slashes
        while '//' in normalized_path:
            normalized_path = normalized_path.replace('//', '/')
        
        # Normalize pattern - ensure forward slashes
        pattern = pattern.replace('\\', '/')
        # Remove any leading ./
        if pattern.startswith('./'):
            pattern = pattern[2:]
        # Remove any duplicate slashes  
        while '//' in pattern:
            pattern = pattern.replace('//', '/')
        
        # Debug output
        show_debug = False
        if self.debug_exclude:
            # Only show debug for files that might match the pattern to reduce noise
            if '**' in pattern and pattern.endswith('/**'):
                # For patterns like "external/**", only show if path contains the directory
                dir_name = pattern[:-3]
                if dir_name in normalized_path:
                    show_debug = True
            elif '**' not in pattern or normalized_path.count('/') <= 3:
                show_debug = True
            
            if show_debug:
                print(f"    DEBUG: Checking '{normalized_path}' against pattern '{pattern}'")
        
        # Handle ** glob pattern
        if '**' in pattern:
            if pattern == '**':
                # Match everything
                return True
            elif pattern.startswith('**/'):
                # Pattern like "**/test" - match anywhere
                search_pattern = pattern[3:]  # Remove **/
                # Check if this pattern appears anywhere in the path as a complete component
                path_parts = normalized_path.split('/')
                for i in range(len(path_parts)):
                    if fnmatch.fnmatch(path_parts[i], search_pattern):
                        if show_debug:
                            print(f"      MATCHED: Component '{path_parts[i]}' matches '{search_pattern}'")
                        return True
                    # Also check subpaths
                    subpath = '/'.join(path_parts[i:])
                    if fnmatch.fnmatch(subpath, search_pattern):
                        if show_debug:
                            print(f"      MATCHED: Subpath '{subpath}' matches '{search_pattern}'")
                        return True
            elif pattern.endswith('/**'):
                # Pattern like "external/**" - match directory and everything under it
                dir_pattern = pattern[:-3]  # Remove /**
                
                # Method 1: Check if any directory component matches the pattern
                path_parts = normalized_path.split('/')
                for i, part in enumerate(path_parts):
                    if fnmatch.fnmatch(part, dir_pattern):
                        # This directory component matches
                        if show_debug:
                            print(f"      MATCHED: Directory component '{part}' matches '{dir_pattern}'")
                        return True
                
                # Method 2: Check if the pattern appears as a complete directory in the path
                # This is more direct - look for /dir_pattern/ in the path
                if f"/{dir_pattern}/" in f"/{normalized_path}/":
                    if show_debug:
                        print(f"      MATCHED: Path contains directory '/{dir_pattern}/'")
                    return True
                
                # Method 3: Check if path starts with the pattern (for relative paths)
                if normalized_path == dir_pattern or normalized_path.startswith(f"{dir_pattern}/"):
                    if show_debug:
                        print(f"      MATCHED: Path starts with '{dir_pattern}'")
                    return True
            else:
                # Pattern with ** in the middle like "src/**/test.cpp"
                # Split pattern by **
                parts = pattern.split('**')
                if len(parts) == 2:
                    start_pattern, end_pattern = parts
                    # Check if path starts with the start pattern and ends with end pattern
                    # Remove trailing/leading slashes
                    start_pattern = start_pattern.rstrip('/')
                    end_pattern = end_pattern.lstrip('/')
                    
                    # Find if start pattern matches
                    if start_pattern:
                        start_found = False
                        for i in range(len(normalized_path)):
                            if normalized_path[:i].endswith(start_pattern):
                                start_found = True
                                remaining = normalized_path[i:]
                                if not end_pattern or fnmatch.fnmatch(remaining.lstrip('/'), end_pattern):
                                    if show_debug:
                                        print(f"      MATCHED: Path matches pattern with ** in middle")
                                    return True
                    else:
                        # No start pattern, just check if ends with end pattern
                        if fnmatch.fnmatch(normalized_path, f"*{end_pattern}"):
                            if show_debug:
                                print(f"      MATCHED: Path ends with '{end_pattern}'")
                            return True
        else:
            # Standard glob pattern without **
            # Direct match
            if fnmatch.fnmatch(normalized_path, pattern):
                if show_debug:
                    print(f"      MATCHED: Direct fnmatch")
                return True
            
            # For patterns like "*.test.cpp", check just the filename
            if '*' in pattern and '/' not in pattern:
                filename = os.path.basename(normalized_path)
                if fnmatch.fnmatch(filename, pattern):
                    if show_debug:
                        print(f"      MATCHED: Filename '{filename}' matches '{pattern}'")
                    return True
            
            # For patterns like "tests/*", check if any parent directory matches
            if pattern.endswith('/*'):
                dir_pattern = pattern[:-2]
                path_parts = normalized_path.split('/')
                for i in range(len(path_parts)):
                    partial_path = '/'.join(path_parts[:i+1])
                    if fnmatch.fnmatch(partial_path, dir_pattern):
                        if show_debug:
                            print(f"      MATCHED: Parent directory matches '{dir_pattern}'")
                        return True
        
        if show_debug:
            print(f"      No match")
        
        return False
    
    def _should_exclude(self, file_path):
        """Check if file should be excluded based on patterns"""
        for pattern in self.exclude_patterns:
            if self._should_exclude_with_pattern(file_path, pattern):
                return True
        return False
    
    def _parse_clang_tidy_output(self, output, current_file=None):
        """Parse clang-tidy output into structured data"""
        pattern = r'(.+):(\d+):(\d+): (warning|error|note): (.+) \[(.+)\]'
        
        if self.debug_parsing:
            print(f"\n{'='*80}")
            print(f"DEBUG: Parsing output for file: {current_file}")
            print(f"Output length: {len(output)} characters")
            print(f"Output preview (first 500 chars):\n{output[:500]}")
            print(f"{'='*80}\n")
        
        lines_processed = 0
        warnings_found = 0
        duplicates_skipped = 0
        excluded_skipped = 0
        potential_warnings = 0
        
        for line in output.split('\n'):
            lines_processed += 1
            
            # Check if line contains warning/error indicators
            if ' warning:' in line or ' error:' in line:
                potential_warnings += 1
            
            match = re.match(pattern, line)
            if match:
                file_path = match.group(1)
                
                if self.debug_parsing:
                    print(f"DEBUG: Found potential warning in line {lines_processed}:")
                    print(f"  File: {file_path}")
                    print(f"  Line: {match.group(2)}, Column: {match.group(3)}")
                    print(f"  Severity: {match.group(4)}")
                    print(f"  Message: {match.group(5)}")
                    print(f"  Check: {match.group(6)}")
                
                # Check if this file should be excluded
                if self.exclude_patterns and self._should_exclude(file_path):
                    excluded_skipped += 1
                    if self.debug_parsing:
                        print(f"  -> SKIPPED: File excluded by patterns")
                    continue
                
                # Create a unique key for this warning to avoid duplicates
                warning_key = (
                    file_path,
                    int(match.group(2)),  # line
                    int(match.group(3)),  # column
                    match.group(4),       # severity
                    match.group(5),       # message
                    match.group(6)        # check
                )
                
                # Skip if we've already seen this exact warning
                if warning_key in self.warnings_set:
                    duplicates_skipped += 1
                    if self.debug_parsing:
                        print(f"  -> SKIPPED: Duplicate warning")
                    continue
                
                self.warnings_set.add(warning_key)
                warnings_found += 1
                
                warning = {
                    'file': file_path,
                    'line': int(match.group(2)),
                    'column': int(match.group(3)),
                    'severity': match.group(4),
                    'message': match.group(5),
                    'check': match.group(6),
                    'timestamp': datetime.now().isoformat()
                }
                self.warnings.append(warning)
                
                if self.debug_parsing:
                    print(f"  -> ADDED: Warning #{len(self.warnings)}")
                
                # Track warnings per file
                if not (self.exclude_patterns and self._should_exclude(file_path)):
                    self.file_warnings[file_path] += 1
        
        if self.debug_parsing:
            print(f"\nDEBUG: Parsing summary for {current_file}:")
            print(f"  Lines processed: {lines_processed}")
            print(f"  Lines with warning/error keywords: {potential_warnings}")
            print(f"  Warnings matching pattern: {warnings_found}")
            print(f"  Duplicates skipped: {duplicates_skipped}")
            print(f"  Excluded files skipped: {excluded_skipped}")
            print(f"  Total warnings so far: {len(self.warnings)}")
            
            # If we found potential warnings but couldn't parse them
            if potential_warnings > 0 and warnings_found == 0:
                print("\n⚠️  WARNING: Found lines with 'warning:' or 'error:' but couldn't parse them!")
                print("  This might indicate a different clang-tidy output format.")
                print("  Sample unparsed lines:")
                for line in output.split('\n')[:50]:
                    if (' warning:' in line or ' error:' in line) and not re.match(pattern, line):
                        print(f"    {line[:120]}...")
            
            print(f"{'='*80}\n")
    
    def _find_clang_tidy_config(self):
        """Find .clang-tidy configuration file"""
        # Check in order: current directory, build directory, parent directories
        search_paths = [
            '.',
            self.build_dir,
            os.path.dirname(self.build_dir),
            os.path.dirname(os.path.dirname(self.build_dir))
        ]
        
        for path in search_paths:
            config_path = os.path.join(path, '.clang-tidy')
            if os.path.exists(config_path):
                return os.path.abspath(config_path)
        
        return None
    
    def _run_clang_tidy_single(self, file_path, checks=None, use_config_file=True):
        """Run clang-tidy on a single file"""
        cmd = ['clang-tidy', '-p', self.build_dir]
        
        # Add header filter if specified
        if self.header_filter:
            cmd.extend(['-header-filter', self.header_filter])
        
        # Check for .clang-tidy config file
        config_file = None
        if use_config_file:
            config_file = self._find_clang_tidy_config()
            if config_file:
                # When using config file, don't override checks unless explicitly specified
                if checks:
                    cmd.extend(['-checks', checks])
                # clang-tidy will automatically use .clang-tidy if found in search path
                if self.print_mode in [PrintMode.VERBOSE, PrintMode.FULL]:
                    print(f"  Using .clang-tidy config from: {config_file}")
            elif not checks:
                # No config file and no checks specified, use sensible defaults
                default_checks = 'clang-diagnostic-*,clang-analyzer-*,google-*,modernize-*,performance-*,readability-*'
                cmd.extend(['-checks', default_checks])
                if self.print_mode in [PrintMode.VERBOSE, PrintMode.FULL]:
                    print(f"  No .clang-tidy file found, using default checks")
            else:
                # No config file, but checks specified
                cmd.extend(['-checks', checks])
        else:
            # Explicitly not using config file
            if not checks:
                # Use minimal defaults when config is disabled and no checks specified
                default_checks = 'clang-diagnostic-*,clang-analyzer-*'
                cmd.extend(['-checks', default_checks])
            else:
                cmd.extend(['-checks', checks])
        
        cmd.append(file_path)
        
        if self.debug_parsing:
            print(f"\nDEBUG: Running command: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        # Store the command for diagnostic purposes
        self.last_command = ' '.join(cmd)
        
        # Check for common errors
        if result.returncode != 0 and self.debug_parsing:
            print(f"DEBUG: clang-tidy returned non-zero exit code: {result.returncode}")
            if "error: no compilation database found" in result.stderr:
                print("DEBUG: ERROR - No compilation database found!")
                print(f"       Looking in: {self.build_dir}")
            elif "error: unable to find" in result.stderr:
                print("DEBUG: ERROR - File not found or compilation issue")
            elif "LLVM ERROR" in result.stderr:
                print("DEBUG: ERROR - LLVM internal error")
        
        if self.debug_parsing:
            print(f"DEBUG: Command exit code: {result.returncode}")
            print(f"DEBUG: stdout length: {len(result.stdout)}")
            print(f"DEBUG: stderr length: {len(result.stderr)}")
            if result.stderr:
                print(f"DEBUG: stderr preview:\n{result.stderr[:500]}")
        
        return result.stdout + result.stderr
    
    def _print_file_output(self, file_path, output):
        """Print clang-tidy output based on print mode"""
        if self.print_mode == PrintMode.QUIET:
            return
            
        # Count issues in output
        warning_count = len(re.findall(r'(warning|error):', output))
        
        # Use project-relative path for display
        display_path = self._get_display_path(file_path)
        
        if self.print_mode == PrintMode.PROGRESS:
            if warning_count > 0:
                print(f"\n  → {display_path}: {warning_count} issues found")
                
        elif self.print_mode == PrintMode.VERBOSE:
            print(f"\n{'='*80}")
            print(f"File: {display_path}")
            print(f"Issues found: {warning_count}")
            if warning_count > 0:
                # Show just the warning/error lines
                for line in output.split('\n'):
                    if re.search(r'(warning|error|note):', line):
                        print(f"  {line}")
            print(f"{'='*80}")
            
        elif self.print_mode == PrintMode.FULL:
            print(f"\n{'='*80}")
            print(f"File: {display_path}")
            print(f"{'='*80}")
            print(output)
            print(f"{'='*80}\n")
    
    def run_analysis(self, checks=None, fix=False, use_config_file=True):
        """Run clang-tidy on all files with progress tracking"""
        self._print_stage("Starting clang-tidy analysis")
        
        # First verify clang-tidy is available
        try:
            test_result = subprocess.run(['clang-tidy', '--version'], 
                                       capture_output=True, text=True)
            if test_result.returncode != 0:
                self._print_stage("clang-tidy not working properly", "FAILED")
                print("Error: clang-tidy returned an error. Run with --test-clang-tidy for diagnostics.")
                return 1
        except FileNotFoundError:
            self._print_stage("clang-tidy not found", "FAILED")
            print("Error: clang-tidy not found in PATH!")
            print("Please install clang-tidy and ensure it's in your PATH.")
            print("Run with --test-clang-tidy for more information.")
            return 1
        
        # Check for .clang-tidy config file
        if use_config_file:
            config_file = self._find_clang_tidy_config()
            if config_file:
                self.config_file_used = config_file
                print(f"  Found .clang-tidy configuration: {config_file}")
                if checks:
                    print(f"  Note: Using both .clang-tidy and command-line checks: {checks}")
                    self.checks_used = checks
            else:
                if not checks:
                    print("  No .clang-tidy file found, using default checks")
                    self.checks_used = 'clang-diagnostic-*,clang-analyzer-*,google-*,modernize-*,performance-*,readability-*'
                else:
                    print(f"  No .clang-tidy file found, using specified checks: {checks}")
                    self.checks_used = checks
        else:
            self.checks_used = checks if checks else 'clang-diagnostic-*,clang-analyzer-*'
        
        # Show header filter information
        if self.header_filter:
            print(f"  Header filter: '{self.header_filter}'")
            if self.header_filter == '.*':
                print("  Note: Including warnings from ALL header files")
            else:
                print(f"  Note: Including warnings from headers matching: {self.header_filter}")
        else:
            print("  Note: Not showing warnings from header files (use --header-filter to include)")
        
        # Check clang-tidy version if in debug mode
        if self.debug_parsing:
            try:
                version_result = subprocess.run(['clang-tidy', '--version'], 
                                              capture_output=True, text=True)
                if version_result.returncode == 0:
                    print(f"\n  Clang-tidy version:")
                    for line in version_result.stdout.strip().split('\n'):
                        print(f"    {line}")
            except:
                pass
        
        # Verify we have files to check
        if not self.files_to_check:
            self._print_stage("No files to analyze!", "FAILED")
            print("Error: No files found to analyze. Check your compile_commands.json and exclusion patterns")
            return 1
            
        total_files = len(self.files_to_check)
        
        # Check if files exist (sample check for debugging)
        if self.print_mode in [PrintMode.VERBOSE, PrintMode.FULL]:
            missing_files = []
            for f in self.files_to_check[:5]:  # Check first 5 files
                if not os.path.exists(f):
                    missing_files.append(f)
            if missing_files:
                print(f"Warning: Some files don't exist: {missing_files}")
        
        # Setup progress bar
        progress = None
        if self.print_mode in [PrintMode.PROGRESS, PrintMode.VERBOSE]:
            if TQDM_AVAILABLE:
                progress = tqdm(total=total_files, desc="Analyzing files", unit="file")
            else:
                progress = ProgressBar(total_files, "Analyzing files")
        
        # Process each file individually
        all_output = []
        files_processed = 0
        files_skipped = 0
        
        # Re-calculate file warnings at the end
        self.file_warnings.clear()
        
        for i, file_path in enumerate(self.files_to_check):
            # Skip if file doesn't exist
            if not os.path.exists(file_path):
                if self.print_mode != PrintMode.QUIET:
                    print(f"\nWarning: File not found: {self._get_display_path(file_path)}")
                files_skipped += 1
                if progress:
                    progress.update(1)
                continue
                
            self.current_file = os.path.basename(file_path)
            files_processed += 1
            
            # Update progress description
            if progress and TQDM_AVAILABLE:
                progress.set_description(f"Analyzing {self.current_file}")
            
            # Run clang-tidy on this file
            output = self._run_clang_tidy_single(file_path, checks, use_config_file)
            all_output.append(output)
            
            # Save raw output if requested
            if self.save_raw_output:
                raw_output_file = self._get_output_path(f"raw_output_{files_processed}_{os.path.basename(file_path)}.txt")
                with open(raw_output_file, 'w') as f:
                    f.write(f"File: {file_path}\n")
                    f.write(f"Command: clang-tidy -p {self.build_dir}")
                    if self.header_filter:
                        f.write(f" -header-filter '{self.header_filter}'")
                    if checks:
                        f.write(f" -checks '{checks}'")
                    f.write(f" {file_path}\n")
                    f.write(f"{'='*80}\n")
                    f.write(output)
                self.raw_outputs.append((file_path, raw_output_file))
            
            # Parse output
            self._parse_clang_tidy_output(output, file_path)
            
            # Print output based on mode
            self._print_file_output(file_path, output)
            
            if progress:
                progress.update(1)
        
        if progress:
            progress.close()
        
        # Recalculate file warnings based on unique warnings
        for warning in self.warnings:
            self.file_warnings[warning['file']] += 1
        
        self._print_stage(f"Analysis complete - Processed {files_processed}/{total_files} files, Found {len(self.warnings)} total issues", "COMPLETED")
        
        if files_skipped > 0:
            print(f"  Note: {files_skipped} files were skipped (not found)")
        
        # If no warnings found and debug mode, save a sample analysis
        if len(self.warnings) == 0:
            if self.debug_parsing or self.save_raw_output:
                print("\n⚠️  No warnings found! Saving diagnostic information...")
                if len(all_output) > 0 and len(self.files_to_check) > 0:
                    diagnostic_file = self._get_output_path("no_warnings_diagnostic.txt")
                    with open(diagnostic_file, 'w') as f:
                        f.write("No warnings found - Diagnostic Information\n")
                        f.write("="*80 + "\n")
                        f.write(f"First file analyzed: {self.files_to_check[0]}\n")
                        f.write(f"Output length: {len(all_output[0]) if all_output else 0} characters\n")
                        f.write(f"Checks used: {self.checks_used}\n")
                        f.write(f"Config file: {self.config_file_used}\n")
                        f.write(f"Header filter: {self.header_filter or 'None'}\n")
                        if hasattr(self, 'last_command'):
                            f.write(f"Last clang-tidy command: {self.last_command}\n")
                        f.write("\nFirst file output:\n")
                        f.write("-"*80 + "\n")
                        if all_output:
                            f.write(all_output[0])
                        f.write("\n\nPossible reasons for no warnings:\n")
                        f.write("1. The code is clean (no issues)\n")
                        f.write("2. The checks are too restrictive\n")
                        f.write("3. Clang-tidy cannot find the includes\n")
                        f.write("4. Different clang-tidy version behavior\n")
                        f.write("5. Compilation database issues\n")
                    print(f"  Diagnostic info saved to: {diagnostic_file}")
            else:
                print("\n✓ Analysis complete - No issues found!")
                print("  (Run with --debug-parsing if you expected warnings)")
        
        return 0
    
    def generate_html_report(self, filename='clang_tidy_report.html', max_issues_per_page=100):
        """Generate optimized HTML report for large projects"""
        output_file = self._get_output_path(filename)
        self._print_stage("Generating optimized HTML report")
        
        # Calculate pagination
        total_issues = len(self.warnings)
        total_pages = (total_issues + max_issues_per_page - 1) // max_issues_per_page
        
        # Group warnings by file
        warnings_by_file = defaultdict(list)
        for w in self.warnings:
            warnings_by_file[w['file']].append(w)
        
        # Generate main report file
        self._generate_html_index(output_file, warnings_by_file, total_issues)
        
        # Generate individual file reports if there are many issues
        if total_issues > 1000:
            self._generate_file_reports(warnings_by_file, output_file)
        
        print(f"  ✓ HTML report saved to: {output_file}")
        if total_issues > 1000:
            print(f"    Note: Generated separate file reports due to large number of issues ({total_issues})")
    
    def _generate_html_index(self, output_file, warnings_by_file, total_issues):
        """Generate the main HTML index with summary and navigation"""
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Clang-Tidy Analysis Report</title>
    <meta charset="UTF-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        h1, h2, h3 {{
            color: #333;
        }}
        .summary {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }}
        .stat-card {{
            background-color: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
            border: 1px solid #e9ecef;
        }}
        .stat-value {{
            font-size: 36px;
            font-weight: bold;
            color: #495057;
        }}
        .stat-label {{
            color: #6c757d;
            margin-top: 5px;
        }}
        .stat-label small {{
            font-size: 11px;
            opacity: 0.8;
        }}
        .warning-box {{
            background-color: #fff3cd;
            border: 1px solid #ffeaa7;
            border-radius: 4px;
            padding: 15px;
            margin: 20px 0;
        }}
        .file-table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }}
        .file-table th, .file-table td {{
            border: 1px solid #dee2e6;
            padding: 12px;
            text-align: left;
        }}
        .file-table th {{
            background-color: #f8f9fa;
            font-weight: bold;
            color: #495057;
            position: sticky;
            top: 0;
            z-index: 10;
        }}
        .file-table tr:nth-child(even) {{
            background-color: #f8f9fa;
        }}
        .file-table tr:hover {{
            background-color: #e9ecef;
        }}
        .file-link {{
            color: #007bff;
            text-decoration: none;
            font-family: monospace;
        }}
        .file-link:hover {{
            text-decoration: underline;
        }}
        .issue-count {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 14px;
            font-weight: bold;
        }}
        .errors {{
            background-color: #f8d7da;
            color: #721c24;
        }}
        .warnings {{
            background-color: #fff3cd;
            color: #856404;
        }}
        .chart-container {{
            margin: 20px 0;
            height: 300px;
        }}
        .collapsible {{
            background-color: #f8f9fa;
            color: #495057;
            cursor: pointer;
            padding: 18px;
            width: 100%;
            border: none;
            text-align: left;
            outline: none;
            font-size: 16px;
            font-weight: bold;
            border-radius: 4px;
            margin: 10px 0;
        }}
        .collapsible:hover {{
            background-color: #e9ecef;
        }}
        .collapsible:after {{
            content: '\\002B';
            color: #495057;
            font-weight: bold;
            float: right;
            margin-left: 5px;
        }}
        .active:after {{
            content: "\\2212";
        }}
        .content {{
            padding: 0 18px;
            max-height: 0;
            overflow: hidden;
            transition: max-height 0.2s ease-out;
            background-color: white;
        }}
        .filter-box {{
            margin: 20px 0;
            padding: 15px;
            background-color: #f8f9fa;
            border-radius: 4px;
        }}
        .filter-input {{
            width: 100%;
            padding: 10px;
            font-size: 16px;
            border: 1px solid #ced4da;
            border-radius: 4px;
        }}
        .check-badge {{
            display: inline-block;
            padding: 4px 8px;
            margin: 2px;
            background-color: #e9ecef;
            color: #495057;
            border-radius: 4px;
            font-size: 12px;
            font-family: monospace;
        }}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <div class="container">
        <h1>🔍 Clang-Tidy Analysis Report</h1>
        <p>Generated on: <strong>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</strong></p>
        <p>Build directory: <code>{self.build_dir}</code></p>
"""
        
        # Add header filter information
        if self.header_filter:
            escaped_filter = html.escape(self.header_filter)
            html_content += f"""
        <p>Header filter: <code>{escaped_filter}</code></p>
"""
        
        # Add exclusion information if patterns were used
        if self.exclude_patterns:
            escaped_patterns = [html.escape(p) for p in self.exclude_patterns]
            html_content += f"""
        <p>Excluded patterns: <code>{', '.join(escaped_patterns)}</code></p>
"""
        
        # Add project directory information if specified
        if self.project_dir:
            html_content += f"""
        <p>Project directory: <code>{html.escape(self.project_dir)}</code></p>
"""
        
        html_content += f"""
        <div class="summary">
            <div class="stat-card">
                <div class="stat-value">{total_issues}</div>
                <div class="stat-label">Total Issues<br><small>(duplicates removed)</small></div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #dc3545;">{len([w for w in self.warnings if w['severity'] == 'error'])}</div>
                <div class="stat-label">Errors</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #ffc107;">{len([w for w in self.warnings if w['severity'] == 'warning'])}</div>
                <div class="stat-label">Warnings</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(warnings_by_file)}</div>
                <div class="stat-label">Files with Issues</div>
            </div>
        </div>
"""
        
        # Add warning for large reports
        if total_issues > 1000:
            html_content += f"""
        <div class="warning-box">
            <strong>⚠️ Large Report:</strong> This project has {total_issues} issues. 
            File-specific reports have been generated for better performance.
            Click on file names below to view detailed issues for each file.
        </div>
"""
        
        # Add check type summary
        check_counts = defaultdict(int)
        for w in self.warnings:
            check_counts[w['check']] += 1
        
        sorted_checks = sorted(check_counts.items(), key=lambda x: x[1], reverse=True)
        
        html_content += """
        <h2>📊 Issues by Check Type</h2>
        <button class="collapsible">Show/Hide Check Types</button>
        <div class="content">
            <canvas id="checkChart"></canvas>
            <div style="margin-top: 20px;">
"""
        
        # Add check badges
        for check, count in sorted_checks[:20]:
            html_content += f'<span class="check-badge">{check} ({count})</span>'
        
        if len(sorted_checks) > 20:
            html_content += f'<span class="check-badge">... and {len(sorted_checks) - 20} more</span>'
        
        html_content += """
            </div>
        </div>
        
        <h2>📁 Files with Issues</h2>
        <div class="filter-box">
            <input type="text" id="fileFilter" class="filter-input" placeholder="Filter files by name..." onkeyup="filterFiles()">
        </div>
        
        <table class="file-table" id="fileTable">
            <thead>
                <tr>
                    <th onclick="sortTable(0)">File Path ↕️</th>
                    <th onclick="sortTable(1)">Errors ↕️</th>
                    <th onclick="sortTable(2)">Warnings ↕️</th>
                    <th onclick="sortTable(3)">Total ↕️</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
"""
        
        # Add file entries
        for file_path, file_warnings in sorted(warnings_by_file.items(), 
                                              key=lambda x: len(x[1]), reverse=True):
            error_count = len([w for w in file_warnings if w['severity'] == 'error'])
            warning_count = len([w for w in file_warnings if w['severity'] == 'warning'])
            total_count = len(file_warnings)
            
            # Use project-relative path for display
            display_path = self._get_display_path(file_path)
            
            # Generate file report name
            file_report = ""
            if total_issues > 1000:
                safe_filename = file_path.replace('/', '_').replace('\\', '_').replace('.', '_')
                safe_filename = re.sub(r'[^\w\-_]', '_', safe_filename)  # Replace any non-alphanumeric chars
                file_report_name = f"{output_file.rsplit('.', 1)[0]}_file_{safe_filename}.html"
                file_report = f'<a href="{os.path.basename(file_report_name)}" class="file-link">View Details</a>'
            else:
                file_report = f'<a href="#{self._make_anchor(file_path)}" class="file-link">Jump to Details</a>'
            
            html_content += f"""
                <tr>
                    <td><code>{html.escape(display_path)}</code></td>
                    <td><span class="issue-count errors">{error_count}</span></td>
                    <td><span class="issue-count warnings">{warning_count}</span></td>
                    <td><strong>{total_count}</strong></td>
                    <td>{file_report}</td>
                </tr>
"""
        
        html_content += """
            </tbody>
        </table>
"""
        
        # Add inline details only if not too many issues
        if total_issues <= 1000:
            html_content += """
        <h2>📝 Detailed Findings</h2>
"""
            for file_path, file_warnings in sorted(warnings_by_file.items()):
                display_path = self._get_display_path(file_path)
                html_content += f"""
        <div id="{self._make_anchor(file_path)}" class="file-section">
            <h3>{html.escape(display_path)}</h3>
"""
                for w in sorted(file_warnings, key=lambda x: (x['line'], x['column']))[:50]:  # Limit to 50 per file
                    html_content += f"""
            <div class="{w['severity']}" style="margin: 10px 0; padding: 10px; border-left: 4px solid;">
                <div style="color: #6c757d; font-size: 14px;">Line {w['line']}, Column {w['column']}</div>
                <div>{html.escape(w['message'])}</div>
                <span class="check-badge">{w['check']}</span>
            </div>
"""
                
                if len(file_warnings) > 50:
                    html_content += f"""
            <div style="padding: 10px; background-color: #f8f9fa; border-radius: 4px;">
                ... and {len(file_warnings) - 50} more issues in this file
            </div>
"""
                html_content += "</div>"
        
        # Add JavaScript
        html_content += """
    </div>
    
    <script>
        // Collapsible sections
        var coll = document.getElementsByClassName("collapsible");
        for (var i = 0; i < coll.length; i++) {
            coll[i].addEventListener("click", function() {
                this.classList.toggle("active");
                var content = this.nextElementSibling;
                if (content.style.maxHeight){
                    content.style.maxHeight = null;
                } else {
                    content.style.maxHeight = content.scrollHeight + "px";
                }
            });
        }
        
        // File filtering
        function filterFiles() {
            var input = document.getElementById("fileFilter");
            var filter = input.value.toUpperCase();
            var table = document.getElementById("fileTable");
            var tr = table.getElementsByTagName("tr");
            
            for (var i = 1; i < tr.length; i++) {
                var td = tr[i].getElementsByTagName("td")[0];
                if (td) {
                    var txtValue = td.textContent || td.innerText;
                    if (txtValue.toUpperCase().indexOf(filter) > -1) {
                        tr[i].style.display = "";
                    } else {
                        tr[i].style.display = "none";
                    }
                }
            }
        }
        
        // Table sorting
        function sortTable(n) {
            var table = document.getElementById("fileTable");
            var rows = Array.from(table.rows).slice(1);
            var ascending = table.getAttribute("data-sort-order") !== "asc";
            
            rows.sort(function(a, b) {
                var aVal = a.cells[n].innerText;
                var bVal = b.cells[n].innerText;
                
                if (n > 0) {  // Numeric columns
                    aVal = parseInt(aVal) || 0;
                    bVal = parseInt(bVal) || 0;
                }
                
                if (ascending) {
                    return aVal > bVal ? 1 : -1;
                } else {
                    return aVal < bVal ? 1 : -1;
                }
            });
            
            table.setAttribute("data-sort-order", ascending ? "asc" : "desc");
            
            var tbody = table.getElementsByTagName("tbody")[0];
            rows.forEach(function(row) {
                tbody.appendChild(row);
            });
        }
"""
        
        # Add chart data (limit to top 10 for performance)
        labels = []
        values = []
        for check, count in sorted_checks[:10]:
            labels.append(check)
            values.append(count)
        
        html_content += f"""
        // Chart data
        var ctx = document.getElementById('checkChart');
        if (ctx) {{
            ctx = ctx.getContext('2d');
            new Chart(ctx, {{
                type: 'bar',
                data: {{
                    labels: {json.dumps(labels)},
                    datasets: [{{
                        label: 'Number of Issues',
                        data: {json.dumps(values)},
                        backgroundColor: 'rgba(54, 162, 235, 0.5)',
                        borderColor: 'rgba(54, 162, 235, 1)',
                        borderWidth: 1
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        y: {{
                            beginAtZero: true
                        }}
                    }}
                }}
            }});
        }}
    </script>
</body>
</html>
"""
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
    
    def _generate_file_reports(self, warnings_by_file, base_output_file):
        """Generate individual HTML reports for each file"""
        base_name = base_output_file.rsplit('.', 1)[0]
        
        for file_path, file_warnings in warnings_by_file.items():
            # Create a safe filename by replacing problematic characters
            safe_filename = file_path.replace('/', '_').replace('\\', '_').replace('.', '_')
            safe_filename = re.sub(r'[^\w\-_]', '_', safe_filename)  # Replace any non-alphanumeric chars
            file_report_name = f"{base_name}_file_{safe_filename}.html"
            
            self._generate_single_file_report(file_report_name, file_path, file_warnings, base_output_file)
    
    def _generate_single_file_report(self, output_file, file_path, warnings, main_report):
        """Generate HTML report for a single file"""
        error_count = len([w for w in warnings if w['severity'] == 'error'])
        warning_count = len([w for w in warnings if w['severity'] == 'warning'])
        
        # Use project-relative path for display
        display_path = self._get_display_path(file_path)
        
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Clang-Tidy Report - {html.escape(display_path)}</title>
    <meta charset="UTF-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .back-link {{
            display: inline-block;
            margin-bottom: 20px;
            color: #007bff;
            text-decoration: none;
        }}
        .back-link:hover {{
            text-decoration: underline;
        }}
        .summary {{
            background-color: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }}
        .warning {{
            background-color: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 15px;
            margin: 10px 0;
            border-radius: 4px;
        }}
        .error {{
            background-color: #f8d7da;
            border-left: 4px solid #dc3545;
            padding: 15px;
            margin: 10px 0;
            border-radius: 4px;
        }}
        .location {{
            color: #6c757d;
            font-size: 14px;
            margin-bottom: 5px;
            font-family: monospace;
        }}
        .message {{
            color: #212529;
            margin: 5px 0;
        }}
        .check-name {{
            color: #0066cc;
            font-size: 12px;
            font-family: monospace;
            background-color: #e9ecef;
            padding: 2px 6px;
            border-radius: 3px;
            display: inline-block;
            margin-top: 5px;
        }}
        .line-group {{
            margin: 20px 0;
            padding: 15px;
            background-color: #f8f9fa;
            border-radius: 8px;
        }}
        .line-header {{
            font-weight: bold;
            margin-bottom: 10px;
            color: #495057;
        }}
    </style>
</head>
<body>
    <div class="container">
        <a href="{os.path.basename(main_report)}" class="back-link">← Back to Summary</a>
        
        <h1>📄 {html.escape(display_path)}</h1>
        
        <div class="summary">
            <strong>Total Issues:</strong> {len(warnings)} 
            (<span style="color: #dc3545;">{error_count} errors</span>, 
            <span style="color: #ffc107;">{warning_count} warnings</span>)
        </div>
        
        <h2>Issues</h2>
"""
        
        # Group warnings by line for better readability
        warnings_by_line = defaultdict(list)
        for w in warnings:
            warnings_by_line[w['line']].append(w)
        
        # Sort by line number
        for line_num in sorted(warnings_by_line.keys()):
            line_warnings = warnings_by_line[line_num]
            
            html_content += f"""
        <div class="line-group">
            <div class="line-header">Line {line_num}</div>
"""
            
            for w in sorted(line_warnings, key=lambda x: x['column']):
                html_content += f"""
            <div class="{w['severity']}">
                <div class="location">Column {w['column']}</div>
                <div class="message">{html.escape(w['message'])}</div>
                <span class="check-name">{w['check']}</span>
            </div>
"""
            
            html_content += "</div>"
        
        html_content += """
    </div>
</body>
</html>
"""
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
    
    def _make_anchor(self, text):
        """Create a valid HTML anchor from text"""
        return re.sub(r'[^\w\-]', '_', text)
    
    def generate_json_report(self, filename='clang_tidy_report.json'):
        """Generate JSON report"""
        output_file = self._get_output_path(filename)
        
        # Calculate excluded file count
        total_files_in_project = len(self.compile_commands) if hasattr(self, 'compile_commands') else len(self.files_to_check)
        excluded_files = total_files_in_project - len(self.files_to_check)
        
        report = {
            'metadata': {
                'timestamp': datetime.now().isoformat(),
                'build_directory': self.build_dir,
                'output_directory': self.output_dir,
                'project_directory': self.project_dir,
                'total_files': total_files_in_project,
                'files_excluded': excluded_files,
                'files_analyzed': len(self.files_to_check),
                'total_warnings': len(self.warnings),
                'total_errors': len([w for w in self.warnings if w['severity'] == 'error']),
                'checks_used': self.checks_used if hasattr(self, 'checks_used') else None,
                'config_file_used': self.config_file_used if hasattr(self, 'config_file_used') else None,
                'exclude_patterns': self.exclude_patterns if self.exclude_patterns else [],
                'header_filter': self.header_filter,
                'duplicates_removed': True  # Always true now since we track unique warnings
            },
            'summary': {
                'by_severity': self._group_by('severity'),
                'by_check': self._group_by('check'),
                'by_file': self._group_by('file')
            },
            'warnings': self.warnings
        }
        
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        print(f"  ✓ JSON report saved to: {output_file}")
    
    def generate_csv_report(self, filename='clang_tidy_report.csv'):
        """Generate CSV report"""
        output_file = self._get_output_path(filename)
        with open(output_file, 'w', newline='') as f:
            if self.warnings:
                fieldnames = self.warnings[0].keys()
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.warnings)
        
        print(f"  ✓ CSV report saved to: {output_file}")
    
    def generate_markdown_report(self, filename='clang_tidy_report.md', max_issues_per_file=500):
        """Generate Markdown report with both summary and detailed findings"""
        output_file = self._get_output_path(filename)
        self._print_stage("Generating Markdown report")
        
        total_issues = len(self.warnings)
        
        # Group warnings by file
        warnings_by_file = defaultdict(list)
        for w in self.warnings:
            warnings_by_file[w['file']].append(w)
        
        # Generate main report file
        self._generate_markdown_index(output_file, warnings_by_file, total_issues)
        
        # Generate individual file reports if there are many issues
        if total_issues > 1000:
            self._generate_markdown_file_reports(warnings_by_file, output_file)
        
        print(f"  ✓ Markdown report saved to: {output_file}")
        if total_issues > 1000:
            print(f"    Note: Generated separate file reports due to large number of issues ({total_issues})")
    
    def _generate_markdown_index(self, output_file, warnings_by_file, total_issues):
        """Generate the main Markdown index with summary and navigation"""
        md = f"""# Clang-Tidy Analysis Report

**Generated on:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**Build Directory:** `{self.build_dir}`  
**Output Directory:** `{self.output_dir}`  
**Header Filter:** `{self.header_filter if self.header_filter else 'None (only source files)'}`
"""
        
        if self.project_dir:
            md += f"**Project Directory:** `{self.project_dir}`\n"
        
        md += f"""
## Summary

- **Total Issues:** {total_issues} (duplicates automatically removed)
- **Errors:** {len([w for w in self.warnings if w['severity'] == 'error'])}
- **Warnings:** {len([w for w in self.warnings if w['severity'] == 'warning'])}
- **Files with Issues:** {len(warnings_by_file)}
"""
        
        if self.exclude_patterns:
            md += f"\n**Excluded Patterns:** `{', '.join(self.exclude_patterns)}`\n"
        
        # Add warning for large reports
        if total_issues > 1000:
            md += f"""
> **⚠️ Large Report:** This project has {total_issues} issues. File-specific reports have been generated for better readability.
> Click on file names below to view detailed issues for each file.
"""
        
        # Add check statistics
        md += "\n## Top Issues by Check Type\n\n"
        
        check_counts = defaultdict(int)
        for w in self.warnings:
            check_counts[w['check']] += 1
        
        sorted_checks = sorted(check_counts.items(), key=lambda x: x[1], reverse=True)
        
        # Show top 20 in table
        md += "| Check | Count | Percentage |\n|-------|-------|------------|\n"
        
        total = total_issues if total_issues else 1
        for check, count in sorted_checks[:20]:
            percentage = (count / total) * 100
            md += f"| `{check}` | {count} | {percentage:.1f}% |\n"
        
        if len(sorted_checks) > 20:
            md += f"\n*... and {len(sorted_checks) - 20} more check types*\n"
        
        # Add file statistics
        md += "\n## Files by Issue Count\n\n"
        
        if total_issues > 1000:
            # For large reports, create a table with links to individual files
            md += "| File | Issues | Errors | Warnings | Details |\n"
            md += "|------|--------|--------|----------|----------|\n"
            
            for file_path, file_warnings in sorted(warnings_by_file.items(), 
                                                  key=lambda x: len(x[1]), reverse=True):
                error_count = len([w for w in file_warnings if w['severity'] == 'error'])
                warning_count = len([w for w in file_warnings if w['severity'] == 'warning'])
                
                # Use project-relative path for display
                display_file = self._get_display_path(file_path)
                
                # Generate file report name
                safe_filename = file_path.replace('/', '_').replace('\\', '_').replace('.', '_')
                safe_filename = re.sub(r'[^\w\-_]', '_', safe_filename)
                file_report_name = f"{output_file.rsplit('.', 1)[0]}_file_{safe_filename}.md"
                
                # URL encode the filename for proper linking
                link_name = urllib.parse.quote(os.path.basename(file_report_name))
                
                md += f"| `{display_file}` | {len(file_warnings)} | {error_count} | {warning_count} | "
                md += f"[View Details](./{link_name}) |\n"
        else:
            # For smaller reports, just show the table without links
            md += "| File | Issues | Errors | Warnings |\n"
            md += "|------|--------|--------|----------|\n"
            
            for file_path, file_warnings in sorted(warnings_by_file.items(), 
                                                  key=lambda x: len(x[1]), reverse=True)[:50]:
                error_count = len([w for w in file_warnings if w['severity'] == 'error'])
                warning_count = len([w for w in file_warnings if w['severity'] == 'warning'])
                
                # Use project-relative path for display
                display_file = self._get_display_path(file_path)
                
                md += f"| `{display_file}` | {len(file_warnings)} | {error_count} | {warning_count} |\n"
            
            if len(warnings_by_file) > 50:
                md += f"\n*... and {len(warnings_by_file) - 50} more files*\n"
        
        # Add inline details only if not too many issues
        if total_issues <= 1000:
            md += "\n## Detailed Findings\n\n"
            
            issues_shown = 0
            max_total_issues = 1000
            max_issues_per_file = 50
            
            # Sort files by number of issues (descending)
            sorted_files = sorted(warnings_by_file.items(), key=lambda x: len(x[1]), reverse=True)
            
            for file_path, file_warnings in sorted_files:
                if issues_shown >= max_total_issues:
                    remaining_files = len(sorted_files) - sorted_files.index((file_path, file_warnings))
                    md += f"\n*... and {remaining_files} more files with issues*\n"
                    break
                    
                # Group by severity
                errors = [w for w in file_warnings if w['severity'] == 'error']
                warnings = [w for w in file_warnings if w['severity'] == 'warning']
                notes = [w for w in file_warnings if w['severity'] == 'note']
                
                # Use project-relative path for display
                display_path = self._get_display_path(file_path)
                
                md += f"\n### `{display_path}`\n\n"
                
                file_issues_shown = 0
                
                if errors and file_issues_shown < max_issues_per_file:
                    md += f"**Errors ({len(errors)}):**\n\n"
                    for w in sorted(errors, key=lambda x: (x['line'], x['column'])):
                        if file_issues_shown >= max_issues_per_file:
                            md += f"- ... and {len(errors) - (file_issues_shown - len(warnings) - len(notes))} more errors\n"
                            break
                        md += f"- **Line {w['line']}, Column {w['column']}**: {w['message']} [`{w['check']}`]\n"
                        issues_shown += 1
                        file_issues_shown += 1
                    md += "\n"
                
                if warnings and file_issues_shown < max_issues_per_file:
                    md += f"**Warnings ({len(warnings)}):**\n\n"
                    warnings_shown = 0
                    for w in sorted(warnings, key=lambda x: (x['line'], x['column'])):
                        if file_issues_shown >= max_issues_per_file:
                            md += f"- ... and {len(warnings) - warnings_shown} more warnings\n"
                            break
                        md += f"- **Line {w['line']}, Column {w['column']}**: {w['message']} [`{w['check']}`]\n"
                        issues_shown += 1
                        file_issues_shown += 1
                        warnings_shown += 1
                    md += "\n"
                
                if file_issues_shown >= max_issues_per_file and (len(errors) + len(warnings) > max_issues_per_file):
                    total_remaining = len(file_warnings) - file_issues_shown
                    if total_remaining > 0:
                        md += f"*... and {total_remaining} more issues in this file*\n\n"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(md)
    
    def _generate_markdown_file_reports(self, warnings_by_file, base_output_file):
        """Generate individual Markdown reports for each file"""
        base_name = base_output_file.rsplit('.', 1)[0]
        
        for file_path, file_warnings in warnings_by_file.items():
            # Create a safe filename by replacing problematic characters
            safe_filename = file_path.replace('/', '_').replace('\\', '_').replace('.', '_')
            safe_filename = re.sub(r'[^\w\-_]', '_', safe_filename)
            file_report_name = f"{base_name}_file_{safe_filename}.md"
            
            self._generate_single_markdown_file_report(file_report_name, file_path, file_warnings, base_output_file)
    
    def _generate_single_markdown_file_report(self, output_file, file_path, warnings, main_report):
        """Generate Markdown report for a single file"""
        error_count = len([w for w in warnings if w['severity'] == 'error'])
        warning_count = len([w for w in warnings if w['severity'] == 'warning'])
        note_count = len([w for w in warnings if w['severity'] == 'note'])
        
        # Use project-relative path for display
        display_path = self._get_display_path(file_path)
        
        # URL encode the main report filename for proper linking
        main_report_link = urllib.parse.quote(os.path.basename(main_report))
        
        md = f"""# Clang-Tidy Report - File Details

[← Back to Summary](./{main_report_link})

## 📄 `{display_path}`

**Generated on:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

### Summary

- **Total Issues:** {len(warnings)}
- **Errors:** {error_count}
- **Warnings:** {warning_count}
- **Notes:** {note_count}

### Issues by Type

"""
        
        # Count issues by check type for this file
        file_check_counts = defaultdict(int)
        for w in warnings:
            file_check_counts[w['check']] += 1
        
        sorted_file_checks = sorted(file_check_counts.items(), key=lambda x: x[1], reverse=True)
        
        if sorted_file_checks:
            md += "| Check | Count |\n"
            md += "|-------|-------|\n"
            for check, count in sorted_file_checks[:10]:
                md += f"| `{check}` | {count} |\n"
            
            if len(sorted_file_checks) > 10:
                md += f"\n*... and {len(sorted_file_checks) - 10} more check types*\n"
        
        md += "\n### Detailed Issues\n\n"
        
        # Group warnings by line for better readability
        warnings_by_line = defaultdict(list)
        for w in warnings:
            warnings_by_line[w['line']].append(w)
        
        # Sort by line number
        for line_num in sorted(warnings_by_line.keys()):
            line_warnings = warnings_by_line[line_num]
            
            md += f"#### Line {line_num}\n\n"
            
            for w in sorted(line_warnings, key=lambda x: x['column']):
                severity_icon = {
                    'error': '❌',
                    'warning': '⚠️',
                    'note': 'ℹ️'
                }.get(w['severity'], '•')
                
                md += f"{severity_icon} **Column {w['column']}** - {w['severity'].capitalize()}\n"
                md += f"   - {w['message']}\n"
                md += f"   - Check: `{w['check']}`\n\n"
        
        # Add navigation
        md += "\n---\n"
        md += f"[← Back to Summary](./{main_report_link})\n"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(md)
    
    def _group_by(self, key):
        """Group warnings by a specific key"""
        groups = defaultdict(list)
        for w in self.warnings:
            groups[w[key]].append(w)
        return {k: len(v) for k, v in groups.items()}
    
    def generate_fix_script(self, filename='apply_fixes.sh'):
        """Generate a script to apply fixes"""
        output_file = self._get_output_path(filename)
        
        # Build the command with the same checks that were used
        fix_cmd = f"run-clang-tidy -p {self.build_dir} -fix"
        if self.checks_used:
            fix_cmd += f" -checks='{self.checks_used}'"
        if self.header_filter:
            fix_cmd += f" -header-filter='{self.header_filter}'"
        fix_cmd += " -j $(nproc)"
        
        # Add note about config file if used
        config_note = ""
        if self.config_file_used:
            config_note = f"# Using .clang-tidy configuration from: {self.config_file_used}\n"
        
        script = f"""#!/bin/bash
# Auto-generated script to apply clang-tidy fixes
# Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

echo "Applying clang-tidy fixes..."
echo "Build directory: {self.build_dir}"
{config_note}
# Create backup
echo "Creating backup..."
tar -czf code_backup_$(date +%Y%m%d_%H%M%S).tar.gz src/ include/

# Apply fixes
echo "Applying fixes..."
{fix_cmd}

echo "Fixes applied! Check git diff to review changes."
"""
        
        with open(output_file, 'w') as f:
            f.write(script)
        
        os.chmod(output_file, 0o755)
        print(f"  ✓ Fix script saved to: {output_file}")

def print_summary(reporter):
    """Print a nice summary at the end"""
    print("\n" + "="*60)
    print("📊 ANALYSIS SUMMARY")
    print("="*60)
    
    error_count = len([w for w in reporter.warnings if w['severity'] == 'error'])
    warning_count = len([w for w in reporter.warnings if w['severity'] == 'warning'])
    
    print(f"Total issues found: {len(reporter.warnings)}")
    print(f"  - Errors: {error_count}")
    print(f"  - Warnings: {warning_count}")
    print(f"Files analyzed: {len(reporter.files_to_check)}")
    print(f"Files with issues: {len(set(w['file'] for w in reporter.warnings))}")
    
    # Show if duplicates were removed
    if hasattr(reporter, 'warnings_set'):
        # In parallel mode, duplicates are common especially with header files
        print(f"\nDuplicate handling:")
        print(f"  - Unique issues tracked (duplicates removed automatically)")
    
    # Show header filter info
    if reporter.header_filter:
        print(f"\nHeader filter: '{reporter.header_filter}'")
        # Count issues from header files
        header_issues = [w for w in reporter.warnings if w['file'].endswith(('.h', '.hpp', '.hxx', '.hh'))]
        if header_issues:
            print(f"Issues from header files: {len(header_issues)}")
    
    # Show exclusion summary if patterns were used
    if hasattr(reporter, 'exclude_patterns') and reporter.exclude_patterns:
        total_files_in_commands = len(reporter.compile_commands) if hasattr(reporter, 'compile_commands') else 0
        if total_files_in_commands > 0:
            excluded_count = total_files_in_commands - len(reporter.files_to_check)
            if excluded_count > 0:
                print(f"\nExclusion summary:")
                print(f"  - Total files in project: {total_files_in_commands}")
                print(f"  - Files excluded: {excluded_count}")
                print(f"  - Files analyzed: {len(reporter.files_to_check)}")
    
    # Show top 5 files with most issues
    if reporter.file_warnings:
        print("\nTop 5 files with most issues:")
        for file, count in sorted(reporter.file_warnings.items(), key=lambda x: x[1], reverse=True)[:5]:
            # Use project-relative path if available
            display_file = reporter._get_display_path(file)
            print(f"  - {display_file}: {count} issues")
    
    print("="*60)

def parse_formats(format_string):
    """Parse comma-separated format string"""
    if format_string.lower() == 'all':
        return ['json', 'csv', 'html', 'markdown']
    
    formats = [f.strip().lower() for f in format_string.split(',')]
    valid_formats = ['json', 'csv', 'html', 'markdown']
    
    for fmt in formats:
        if fmt not in valid_formats:
            print(f"Warning: Invalid format '{fmt}'. Valid formats are: {', '.join(valid_formats)}")
    
    return [f for f in formats if f in valid_formats]

def parse_exclude_patterns(exclude_string):
    """Parse exclude patterns from comma-separated string"""
    if not exclude_string:
        return []
    
    patterns = [p.strip() for p in exclude_string.split(',')]
    return patterns

def generate_sample_clang_tidy_config():
    """Generate a sample .clang-tidy configuration file"""
    config = """---
# Sample .clang-tidy configuration file
# Generated by clang_tidy_full_report.py

# Checks: specify which checks to enable
# Use -* to disable all default checks first
Checks: '-*,
  clang-diagnostic-*,
  clang-analyzer-*,
  google-*,
  modernize-*,
  performance-*,
  readability-*,
  -google-readability-todo,
  -modernize-use-trailing-return-type,
  -readability-magic-numbers'

# Configure specific check options
CheckOptions:
  - key: readability-identifier-naming.ClassCase
    value: CamelCase
  - key: readability-identifier-naming.ClassMemberCase
    value: lower_case
  - key: readability-identifier-naming.ClassMemberSuffix
    value: '_'
  - key: readability-identifier-naming.ClassMethodCase
    value: CamelCase
  - key: readability-identifier-naming.ConstexprVariableCase
    value: CamelCase
  - key: readability-identifier-naming.ConstexprVariablePrefix
    value: 'k'
  - key: readability-identifier-naming.EnumCase
    value: CamelCase
  - key: readability-identifier-naming.EnumConstantCase
    value: CamelCase
  - key: readability-identifier-naming.EnumConstantPrefix
    value: 'k'
  - key: readability-identifier-naming.FunctionCase
    value: CamelCase
  - key: readability-identifier-naming.GlobalConstantCase
    value: CamelCase
  - key: readability-identifier-naming.GlobalConstantPrefix
    value: 'k'
  - key: readability-identifier-naming.NamespaceCase
    value: lower_case
  - key: readability-identifier-naming.ParameterCase
    value: lower_case
  - key: readability-identifier-naming.PrivateMemberSuffix
    value: '_'
  - key: readability-identifier-naming.StructCase
    value: CamelCase
  - key: readability-identifier-naming.TemplateParameterCase
    value: CamelCase
  - key: readability-identifier-naming.TypedefCase
    value: CamelCase
  - key: readability-identifier-naming.UnionCase
    value: CamelCase
  - key: readability-identifier-naming.VariableCase
    value: lower_case

# WarningsAsErrors: treat these checks as errors
# WarningsAsErrors: 'clang-diagnostic-*,clang-analyzer-*'

# HeaderFilterRegex: which headers to analyze
# HeaderFilterRegex: '.*'

# SystemHeaders: whether to display errors from system headers
SystemHeaders: false

# FormatStyle: specify the style for fixes
FormatStyle: google

# User-specific notes:
# - Uncomment WarningsAsErrors to treat certain warnings as errors
# - Adjust HeaderFilterRegex to include/exclude specific headers
# - Modify Checks to enable/disable specific check categories
# - Update CheckOptions to match your project's naming conventions
# - Note: When using parallel analysis, the script automatically removes duplicate
#   warnings that may occur when the same header is analyzed multiple times
"""
    
    if os.path.exists('.clang-tidy'):
        print("Error: .clang-tidy already exists in the current directory")
        print("Move or rename it first if you want to generate a new one")
        return False
    
    with open('.clang-tidy', 'w') as f:
        f.write(config)
    
    print("Generated .clang-tidy configuration file")
    print("\nTo customize:")
    print("1. Edit the 'Checks' section to enable/disable specific checks")
    print("2. Modify 'CheckOptions' to match your project's naming conventions")
    print("3. Set 'HeaderFilterRegex' to control which headers are analyzed")
    print("4. Uncomment 'WarningsAsErrors' to treat certain warnings as errors")
    print("\nFor more information, see:")
    print("https://clang.llvm.org/extra/clang-tidy/")
    
    return True


def test_clang_tidy():
    """Test if clang-tidy is working correctly"""
    print("Testing clang-tidy installation and functionality...")
    print("="*60)
    
    # Test 1: Check if clang-tidy is available
    print("\n1. Checking clang-tidy availability...")
    try:
        result = subprocess.run(['clang-tidy', '--version'], capture_output=True, text=True)
        if result.returncode == 0:
            print("✓ clang-tidy found")
            print(f"Version info:\n{result.stdout}")
        else:
            print("✗ Error running clang-tidy")
            print(f"Error: {result.stderr}")
            return False
    except FileNotFoundError:
        print("✗ clang-tidy not found in PATH")
        print("Please install clang-tidy and ensure it's in your PATH")
        return False
    
    # Test 2: Create a test C++ file with known issues
    print("\n2. Creating test file with known issues...")
    test_code = '''#include <iostream>
#include <string>

class test_class {
public:
    int publicMember;  // Should trigger naming convention warning
    
    void TestMethod() {  // Should trigger naming convention warning
        int CamelCaseVar = 5;  // Should trigger naming convention warning
        std::string str = "test";
        if (str.c_str() == "test") {  // Should trigger string comparison warning
            std::cout << "test" << std::endl;
        }
    }
private:
    int privateMember;  // Should trigger naming convention warning
};

int main() {
    test_class obj;
    obj.TestMethod();
    return 0;
}
'''
    
    test_file = "clang_tidy_test.cpp"
    with open(test_file, 'w') as f:
        f.write(test_code)
    print(f"✓ Created test file: {test_file}")
    
    # Test 3: Run clang-tidy with specific checks
    print("\n3. Running clang-tidy with specific checks...")
    try:
        cmd = ['clang-tidy', test_file, '--', '-std=c++11']
        print(f"Command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        output = result.stdout + result.stderr
        print(f"\nOutput length: {len(output)} characters")
        
        if "warning:" in output or "error:" in output:
            print("✓ clang-tidy found issues (as expected)")
            warnings = len([line for line in output.split('\n') if 'warning:' in line])
            errors = len([line for line in output.split('\n') if 'error:' in line])
            print(f"  Warnings: {warnings}")
            print(f"  Errors: {errors}")
            print("\nSample output:")
            print("-"*60)
            lines = output.split('\n')
            for i, line in enumerate(lines[:20]):  # Show first 20 lines
                if line.strip():
                    print(line)
            if len(lines) > 20:
                print(f"... ({len(lines) - 20} more lines)")
        else:
            print("⚠️  No warnings found - this might indicate an issue")
            print("Full output:")
            print(output)
    
    except Exception as e:
        print(f"✗ Error running clang-tidy: {e}")
        return False
    
    finally:
        # Clean up
        if os.path.exists(test_file):
            os.remove(test_file)
            print(f"\n✓ Cleaned up test file")
    
    # Test 4: Test with compile_commands.json if build dir provided
    if len(sys.argv) > 2:
        build_dir = sys.argv[2]
        print(f"\n4. Testing with compile_commands.json from: {build_dir}")
        compile_commands_path = os.path.join(build_dir, "compile_commands.json")
        
        if os.path.exists(compile_commands_path):
            print(f"✓ Found compile_commands.json")
            try:
                # Create test file in temp directory
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w', suffix='.cpp', delete=False) as tf:
                    tf.write(test_code)
                    temp_file = tf.name
                
                cmd = ['clang-tidy', '-p', build_dir, temp_file]
                print(f"Command: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True)
                
                if result.returncode == 0 or "warning:" in result.stdout:
                    print("✓ clang-tidy works with compile_commands.json")
                else:
                    print("⚠️  Issues running with compile_commands.json")
                    if result.stderr:
                        print(f"Error: {result.stderr[:200]}")
                
                os.unlink(temp_file)
            except Exception as e:
                print(f"✗ Error testing with compile_commands.json: {e}")
        else:
            print(f"✗ No compile_commands.json found at: {compile_commands_path}")
    else:
        print("\n4. Skipping compile_commands.json test (no build directory provided)")
        print("   Run with: ./clang_tidy_full_report.py --test-clang-tidy <build-dir>")
    
    # Test 5: Check for common issues
    print("\n5. Checking for common issues...")
    
    # Check if run-clang-tidy is available (for parallel mode)
    try:
        result = subprocess.run(['run-clang-tidy', '--help'], capture_output=True, text=True)
        if result.returncode == 0 or 'usage:' in result.stderr.lower():
            print("✓ run-clang-tidy found (parallel mode available)")
        else:
            print("⚠️  run-clang-tidy not found (parallel mode may not work)")
    except FileNotFoundError:
        print("⚠️  run-clang-tidy not found (parallel mode may not work)")
    
    print("\n" + "="*60)
    print("Testing complete!")
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Generate comprehensive clang-tidy reports with output options',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Print modes:
  quiet    - No output during analysis
  progress - Show progress bar and file counts (default)
  verbose  - Show each file and its warnings/errors
  full     - Show complete clang-tidy output for each file

Format options:
  all              - Generate all formats (default)
  json             - JSON report only
  csv              - CSV report only
  html             - HTML report only
  markdown         - Markdown report only
  html,markdown    - Multiple formats (comma-separated)

Configuration:
  By default, the script looks for a .clang-tidy file in:
    1. Current directory
    2. Build directory  
    3. Parent directories
  
  Use --checks to specify additional checks or override the config file.
  Use --no-config to ignore .clang-tidy files completely.

Header files:
  By default, clang-tidy only shows warnings from source files.
  Use --header-filter to include warnings from header files:
  --header-filter='.*'               - Include ALL headers
  --header-filter='include/.*'       - Headers in 'include' directory
  --header-filter='src/.*\\.h

if __name__ == '__main__':
    sys.exit(main())
      - .h files in 'src' directory
  --header-filter='(?!external).*'   - All except 'external' directory

Exclude patterns:
  Use glob patterns to exclude files or directories:
  --exclude="external/**"              - Exclude all files under any directory named 'external'
  --exclude="**/test/**"               - Exclude all directories named 'test' anywhere
  --exclude="**/*test.cpp"             - Exclude all files ending with 'test.cpp' anywhere
  --exclude="*.tmp"                    - Exclude all .tmp files in any directory
  --exclude="external/**,**/*test.cpp" - Multiple patterns (comma-separated)
  
  Note: Patterns match against the full normalized path of each file.
  Special characters in directory names (like +, -, ., etc.) are matched literally.
  Use ? to match any single character, * to match any characters within a directory.
  The pattern "external/**" will match:
    - external/file.cpp
    - src/external/lib/file.cpp
    - /absolute/path/external/file.cpp
    - external/googletest+/src/file.cpp
    - path/to/external/other/file.cpp

Examples:
  %(prog)s build/                                     # Use .clang-tidy if found
  %(prog)s build/ --header-filter='.*'               # Include all headers
  %(prog)s build/ --header-filter='include/.*\\.h

if __name__ == '__main__':
    sys.exit(main())
  # Only .h files in include/
  %(prog)s build/ --checks="-*,google-*"             # Override checks
  %(prog)s build/ --no-config --checks="google-*"    # Ignore .clang-tidy
  %(prog)s --generate-config                          # Generate sample .clang-tidy file
  %(prog)s build/ --output reports/                  # Save reports to reports/ directory
  %(prog)s build/ --print verbose                    # Show warnings for each file
  %(prog)s build/ --format=html,markdown             # Generate HTML and Markdown only
  %(prog)s build/ --exclude="external/**,tests/**"   # Exclude external and tests directories
  %(prog)s build/ --exclude="external/**,**/googletest/**,**/*_test.cpp"  # Multiple exclusions
  %(prog)s build/ --output results/ --print quiet    # Save to results/, no console output
  %(prog)s build/ --debug --limit 10                 # Debug first 10 files
  %(prog)s build/ --exclude="external/**" --debug-exclude  # Debug pattern matching
  %(prog)s build/ --exclude="external/**,third_party/**,**/googletest/**"  # Multiple directories
  %(prog)s build/ --project-dir /path/to/project     # Use relative paths from project directory
  %(prog)s build/ --parallel                          # Use 2/3 of CPU cores for parallel processing
  %(prog)s build/ --parallel --jobs 8                # Use exactly 8 cores
  
  # Combine header filter with exclusions:
  %(prog)s build/ --header-filter='.*' --exclude="external/**"
  
  # Test if a specific file would be excluded:
  %(prog)s build/ --exclude="external/**" --test-exclude "external/googletest/src/gtest.cc"
        """
    )
    
    parser.add_argument('build_dir', help='Path to build directory with compile_commands.json')
    parser.add_argument('--checks', default=None, 
                        help='Clang-tidy checks to run (if not specified, uses .clang-tidy file or defaults)')
    parser.add_argument('--no-config', action='store_true',
                        help='Ignore .clang-tidy configuration file and use only command-line checks')
    parser.add_argument('--header-filter', default=None,
                        help='Regular expression matching header files to include in output (e.g., ".*" for all headers)')
    parser.add_argument('--format', default='all', 
                        help='Output format(s): all, json, csv, html, markdown, or comma-separated')
    parser.add_argument('--fix', action='store_true', help='Apply fixes automatically')
    parser.add_argument('--print', choices=['quiet', 'progress', 'verbose', 'full'],
                        default='progress', help='Console print mode during analysis')
    parser.add_argument('--output', default='.', help='Output directory for report files')
    parser.add_argument('--project-dir', help='Project directory for relative path display in reports')
    parser.add_argument('--parallel', action='store_true', 
                        help='Use parallel processing (default: 2/3 of CPU cores)')
    parser.add_argument('--jobs', type=int, help='Number of parallel jobs (overrides default 2/3 of cores)')
    parser.add_argument('--debug', action='store_true', help='Show debug information')
    parser.add_argument('--debug-exclude', action='store_true', help='Show detailed exclude pattern matching')
    parser.add_argument('--debug-parsing', action='store_true', help='Show detailed parsing debug information')
    parser.add_argument('--save-raw-output', action='store_true', help='Save raw clang-tidy output to files')
    parser.add_argument('--test-exclude', metavar='PATH', 
                        help='Test if a specific path would be excluded by the patterns (exits after test)')
    parser.add_argument('--limit', type=int, help='Limit analysis to first N files (for testing)')
    parser.add_argument('--test-clang-tidy', action='store_true',
                        help='Test if clang-tidy is working correctly on this system')
    parser.add_argument('--generate-config', action='store_true',
                        help='Generate a sample .clang-tidy configuration file and exit')
    parser.add_argument('--exclude', help='Comma-separated patterns to exclude files/directories')
    
    args = parser.parse_args()
    
    # Handle --test-clang-tidy option
    if args.test_clang_tidy:
        return 0 if test_clang_tidy() else 1
    
    # Handle --generate-config option
    if args.generate_config:
        return 0 if generate_sample_clang_tidy_config() else 1
    
    # Parse formats
    formats = parse_formats(args.format)
    if not formats:
        print("Error: No valid formats specified")
        return 1
    
    # Parse exclude patterns
    exclude_patterns = parse_exclude_patterns(args.exclude)
    
    # Test exclude functionality if requested
    if args.test_exclude:
        print(f"Testing exclude patterns against path: {args.test_exclude}")
        if not exclude_patterns:
            print("No exclude patterns specified. Use --exclude to specify patterns.")
            return 0
            
        print(f"Patterns: {exclude_patterns}")
        print("-" * 60)
        
        # Test the path using a minimal instance
        test_reporter = type('TestReporter', (), {
            'exclude_patterns': exclude_patterns,
            'debug_exclude': True,
            '_should_exclude_with_pattern': ClangTidyReporter._should_exclude_with_pattern
        })()
        
        # Test the path
        normalized_test_path = os.path.normpath(args.test_exclude).replace('\\', '/')
        print(f"Normalized path: {normalized_test_path}")
        print("-" * 60)
        
        excluded = False
        for pattern in exclude_patterns:
            if test_reporter._should_exclude_with_pattern(normalized_test_path, pattern):
                print(f"\n✗ Path WOULD BE EXCLUDED by pattern '{pattern}'")
                excluded = True
                break
        
        if not excluded:
            print(f"\n✓ Path WOULD BE INCLUDED (not matched by any pattern)")
        
        return 0
    
    # Print header
    if args.print != PrintMode.QUIET:
        print("🔧 Clang-Tidy Report Generator v3.0")
        print("="*60)
    
    reporter = ClangTidyReporter(
        args.build_dir, 
        print_mode=args.print, 
        output_dir=args.output,
        exclude_patterns=exclude_patterns,
        debug_exclude=args.debug_exclude,
        header_filter=args.header_filter,
        project_dir=args.project_dir,
        debug_parsing=args.debug_parsing,
        save_raw_output=args.save_raw_output
    )
    
    # Apply file limit if specified
    if args.limit and args.limit > 0:
        original_count = len(reporter.files_to_check)
        reporter.files_to_check = reporter.files_to_check[:args.limit]
        print(f"Limiting analysis to first {args.limit} files (out of {original_count})")
    
    # Debug mode: show compile_commands.json information
    if args.debug:
        print("\nDEBUG: Compile Commands Information")
        print(f"Build directory: {os.path.abspath(args.build_dir)}")
        print(f"Output directory: {os.path.abspath(reporter.output_dir)}")
        if args.project_dir:
            print(f"Project directory: {os.path.abspath(args.project_dir)}")
        print(f"Number of files to check: {len(reporter.files_to_check)}")
        print(f"Header filter: {args.header_filter if args.header_filter else 'None (source files only)'}")
        
        # Check for .clang-tidy config
        config_file = reporter._find_clang_tidy_config()
        if config_file:
            print(f"\n.clang-tidy configuration found: {config_file}")
            if args.no_config:
                print("  (Will be ignored due to --no-config flag)")
            else:
                print("  (Will be used for analysis)")
        else:
            print("\nNo .clang-tidy configuration file found")
            if args.checks:
                print(f"  Using command-line checks: {args.checks}")
            else:
                print("  Using clang-tidy defaults")
        
        if exclude_patterns:
            print(f"\nExclusion patterns: {exclude_patterns}")
            print("\nPattern matching examples:")
            print("  'external/**'     - Excludes all files under any 'external' directory")
            print("  '**/*test.cpp'    - Excludes all files ending with 'test.cpp'")
            print("  '*.tmp'           - Excludes all .tmp files")
            print("  'build/*'         - Excludes files directly in 'build' directory")
        
        if reporter.files_to_check:
            print("\nFirst 10 files to be analyzed:")
            for i, file_path in enumerate(reporter.files_to_check[:10]):
                exists = "✓" if os.path.exists(file_path) else "✗"
                display_path = reporter._get_display_path(file_path)
                print(f"  {exists} {display_path}")
            
            # Check how many files actually exist
            existing_files = sum(1 for f in reporter.files_to_check if os.path.exists(f))
            print(f"\nFiles that exist: {existing_files}/{len(reporter.files_to_check)}")
            
            if existing_files == 0:
                print("\nERROR: No files exist! Check if:")
                print("  1. You're running from the correct directory")
                print("  2. The paths in compile_commands.json are correct")
                print("  3. The source files haven't been moved/deleted")
                
                # Show current working directory
                print(f"\nCurrent working directory: {os.getcwd()}")
                
                # Try to guess the issue
                first_file = reporter.files_to_check[0]
                print(f"\nFirst file path: {first_file}")
                if os.path.isabs(first_file):
                    print("  (This is an absolute path)")
                else:
                    print("  (This is a relative path)")
                    
                return 1
        print("\n" + "="*60 + "\n")
    
    start_time = time.time()
    
    try:
        # Note: Parallel mode doesn't support verbose/full print modes
        if args.parallel and args.print in [PrintMode.VERBOSE, PrintMode.FULL]:
            print("Warning: Parallel mode doesn't support verbose/full print modes. Using progress mode.")
            reporter.print_mode = PrintMode.PROGRESS
        
        # Run analysis
        if args.parallel:
            # For parallel mode, we need to filter files first
            print("Running in parallel mode...")
            
            # Determine number of jobs
            if args.jobs:
                num_jobs = args.jobs
            else:
                # Default to 2/3 of CPU cores
                cpu_count = multiprocessing.cpu_count()
                num_jobs = max(1, int(cpu_count * 2 / 3))
            
            print(f"  Using {num_jobs} parallel jobs (out of {multiprocessing.cpu_count()} CPU cores)")
            
            # Check for .clang-tidy config
            if not args.no_config:
                config_file = reporter._find_clang_tidy_config()
                if config_file:
                    print(f"Found .clang-tidy configuration: {config_file}")
                    reporter.config_file_used = config_file
                    if args.checks:
                        reporter.checks_used = args.checks
                elif not args.checks:
                    # No config and no checks, use defaults
                    reporter.checks_used = 'clang-diagnostic-*,clang-analyzer-*,google-*,modernize-*,performance-*,readability-*'
                else:
                    reporter.checks_used = args.checks
            else:
                reporter.checks_used = args.checks if args.checks else 'clang-diagnostic-*,clang-analyzer-*'
            
            # run-clang-tidy doesn't have a direct way to specify a file list,
            # so we'll use it with explicit file arguments when possible
            cmd = ['run-clang-tidy', '-p', args.build_dir]
            if args.checks:
                cmd.extend(['-checks', args.checks])
            elif args.no_config:
                # Use default minimal checks if no config and no checks specified
                cmd.extend(['-checks', 'clang-diagnostic-*,clang-analyzer-*'])
            if args.header_filter:
                cmd.extend(['-header-filter', args.header_filter])
            cmd.extend(['-j', str(num_jobs)])
            
            # Add files as arguments (run-clang-tidy accepts file patterns/paths)
            # If too many files, we might hit command line limits
            if len(reporter.files_to_check) > 500:
                # For very large file lists, just run on all and filter output
                print(f"  Processing {len(reporter.files_to_check)} files in parallel...")
                print(f"  Note: Large number of files - will filter excluded files from results")
                # Don't add file arguments, let it process all
            else:
                # For manageable lists, pass files directly
                cmd.extend(reporter.files_to_check)
            
            # Run with real-time output parsing
            print("\n  Starting analysis...")
            all_output = []
            files_being_processed = set()
            last_status_lines_count = 0
            current_file = ""
            
            # Check if terminal supports ANSI escape codes
            supports_ansi = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty() and os.name != 'nt'
            
            try:
                # Use Popen for real-time output processing
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                         text=True, bufsize=1, universal_newlines=True)
                
                # Parse output line by line
                for line in process.stdout:
                    all_output.append(line)
                    
                    # Look for file processing indicators in the output
                    # run-clang-tidy output includes lines like:
                    # "Processing file: /path/to/file.cpp"
                    # or just the file path when running clang-tidy
                    
                    # Check if this line indicates a file being processed
                    # Look for common patterns in clang-tidy output
                    file_match = None
                    
                    # Pattern 1: Direct file path at start of line
                    if line.strip() and '/' in line and not line.startswith(' '):
                        potential_file = line.strip().split()[0]
                        if potential_file.endswith(('.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.hxx')):
                            file_match = potential_file
                    
                    # Pattern 2: "Processing" or similar keywords
                    if 'rocessing' in line or 'nalyzing' in line:
                        # Extract file path from the line
                        parts = line.split()
                        for part in parts:
                            if '/' in part and part.endswith(('.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.hxx')):
                                file_match = part.strip(':')
                                break
                    
                    # Pattern 3: Clang-tidy warning/error output
                    warning_match = re.match(r'^([^:]+\.(cpp|cc|cxx|c|h|hpp|hxx)):\d+:\d+:', line)
                    if warning_match:
                        file_match = warning_match.group(1)
                    
                    if file_match:
                        # Add to set of files being processed
                        files_being_processed.add(file_match)
                        current_file = file_match
                        
                        # Update status display
                        if reporter.print_mode != PrintMode.QUIET:
                            if supports_ansi:
                                # Move cursor up to overwrite previous status lines
                                if last_status_lines_count > 0:
                                    # Move up and clear lines
                                    for _ in range(last_status_lines_count):
                                        print('\033[1A\033[2K', end='')
                                
                                # Print file count on first line
                                count_line = f"  Files analyzed: {len(files_being_processed)}"
                                print(count_line)
                                
                                # Print current file on second line
                                display_path = reporter._get_display_path(current_file)
                                file_line = f"  Processing: {display_path}"
                                
                                # Truncate if too long
                                terminal_width = 120  # Conservative terminal width
                                if len(file_line) > terminal_width - 10:
                                    file_line = file_line[:terminal_width-13] + "..."
                                
                                print(file_line, flush=True)
                                
                                # Remember we printed 2 lines
                                last_status_lines_count = 2
                            else:
                                # Fallback for terminals without ANSI support
                                # Print progress every 10 files to avoid too much output
                                if len(files_being_processed) % 10 == 0:
                                    display_path = reporter._get_display_path(current_file)
                                    print(f"  Files analyzed: {len(files_being_processed)} - Processing: {display_path}")
                
                # Wait for process to complete
                process.wait()
                return_code = process.returncode
                
                # Clear the status lines and print final count
                if last_status_lines_count > 0 and supports_ansi:
                    # Move up and clear lines
                    for _ in range(last_status_lines_count):
                        print('\033[1A\033[2K', end='')
                
                print(f"  Completed analyzing {len(files_being_processed)} files")
                
            except KeyboardInterrupt:
                process.terminate()
                raise
            
            # Join all output
            full_output = ''.join(all_output)
            
            # Track initial count for duplicate detection
            initial_warnings_count = len(reporter.warnings)
            
            # Parse output - the exclusion filtering happens in _parse_clang_tidy_output
            reporter._parse_clang_tidy_output(full_output)
            
            # Recalculate file warnings based on unique warnings
            reporter.file_warnings.clear()
            for warning in reporter.warnings:
                reporter.file_warnings[warning['file']] += 1
            
            # Calculate how many duplicates were found
            # This is an estimate based on if we see the same warning multiple times
            # In practice, the deduplication happens automatically during parsing
            
            # Show summary of filtering
            if reporter.print_mode != PrintMode.QUIET:
                print(f"  Note: Duplicate warnings automatically removed (common with header files)")
                if reporter.exclude_patterns:
                    print(f"  Note: Warnings from excluded files have been filtered from results")
            
            # Save diagnostic info if no warnings found
            if len(reporter.warnings) == 0 and (reporter.debug_parsing or reporter.save_raw_output):
                print("\n⚠️  No warnings found in parallel mode!")
                diagnostic_file = reporter._get_output_path("no_warnings_parallel_diagnostic.txt")
                with open(diagnostic_file, 'w') as f:
                    f.write("No warnings found in parallel mode - Diagnostic Information\n")
                    f.write("="*80 + "\n")
                    f.write(f"Files processed: {len(files_being_processed)}\n")
                    f.write(f"Output length: {len(full_output)} characters\n")
                    f.write(f"Checks used: {reporter.checks_used}\n")
                    f.write(f"Config file: {reporter.config_file_used}\n")
                    f.write(f"\nFirst 2000 characters of output:\n")
                    f.write("-"*80 + "\n")
                    f.write(full_output[:2000])
                print(f"  Diagnostic info saved to: {diagnostic_file}")
        else:
            return_code = reporter.run_analysis(
                checks=args.checks, 
                fix=args.fix,
                use_config_file=not args.no_config
            )
        
        # Save debug summary before generating reports
        if args.debug_parsing or args.save_raw_output or (args.debug and len(reporter.warnings) == 0):
            debug_summary_file = reporter._get_output_path("debug_summary.txt")
            with open(debug_summary_file, 'w') as f:
                f.write(f"Clang-Tidy Debug Summary\n")
                f.write(f"{'='*80}\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Build directory: {args.build_dir}\n")
                f.write(f"Project directory: {args.project_dir or 'Not specified'}\n")
                f.write(f"Header filter: {args.header_filter or 'None'}\n")
                f.write(f"Exclude patterns: {exclude_patterns}\n")
                f.write(f"Parallel mode: {args.parallel}\n")
                if args.parallel and args.jobs:
                    f.write(f"Jobs: {args.jobs}\n")
                
                # Try to get clang-tidy version
                try:
                    version_result = subprocess.run(['clang-tidy', '--version'], 
                                                  capture_output=True, text=True)
                    f.write(f"\nClang-tidy version:\n{version_result.stdout}\n")
                except:
                    f.write(f"\nClang-tidy version: Unable to determine\n")
                
                # Environment info
                f.write(f"\nEnvironment:\n")
                f.write(f"  Python version: {sys.version}\n")
                f.write(f"  Platform: {sys.platform}\n")
                f.write(f"  Current working directory: {os.getcwd()}\n")
                
                # Config file info
                if hasattr(reporter, 'config_file_used') and reporter.config_file_used:
                    f.write(f"\n.clang-tidy config file used: {reporter.config_file_used}\n")
                    try:
                        with open(reporter.config_file_used, 'r') as config_f:
                            f.write(f"Config content:\n{'-'*40}\n")
                            f.write(config_f.read())
                            f.write(f"\n{'-'*40}\n")
                    except:
                        f.write(f"Unable to read config file\n")
                
                f.write(f"\nFiles to check: {len(reporter.files_to_check)}\n")
                f.write(f"Total warnings found: {len(reporter.warnings)}\n")
                f.write(f"Unique warnings (after deduplication): {len(reporter.warnings_set)}\n")
                f.write(f"\n{'='*80}\n")
                f.write(f"Warnings by file:\n")
                for file_path, count in sorted(reporter.file_warnings.items(), key=lambda x: x[1], reverse=True):
                    f.write(f"  {reporter._get_display_path(file_path)}: {count}\n")
                f.write(f"\n{'='*80}\n")
                f.write(f"First 10 warnings:\n")
                for i, warning in enumerate(reporter.warnings[:10]):
                    f.write(f"\n[{i+1}] {warning['file']}:{warning['line']}:{warning['column']}\n")
                    f.write(f"    Severity: {warning['severity']}\n")
                    f.write(f"    Message: {warning['message']}\n")
                    f.write(f"    Check: {warning['check']}\n")
                
                if reporter.save_raw_output and hasattr(reporter, 'raw_outputs') and reporter.raw_outputs:
                    f.write(f"\n{'='*80}\n")
                    f.write(f"Raw output files saved:\n")
                    for file_path, output_file in reporter.raw_outputs:
                        f.write(f"  {reporter._get_display_path(file_path)} -> {output_file}\n")
            
            print(f"\n📝 Debug summary saved to: {debug_summary_file}")
            
            # Also save warnings to JSON for easy analysis
            if len(reporter.warnings) > 0:
                warnings_debug_file = reporter._get_output_path("warnings_debug.json")
                with open(warnings_debug_file, 'w') as f:
                    json.dump({
                        'total_warnings': len(reporter.warnings),
                        'warnings': reporter.warnings[:100],  # First 100 warnings
                        'file_summary': dict(reporter.file_warnings)
                    }, f, indent=2)
                print(f"📝 Warnings debug data saved to: {warnings_debug_file}")
        
        # Generate reports
        reporter._print_stage(f"Generating {len(formats)} report(s): {', '.join(formats)}")
        
        for fmt in formats:
            if fmt == 'json':
                reporter.generate_json_report()
            elif fmt == 'csv':
                reporter.generate_csv_report()
            elif fmt == 'html':
                reporter.generate_html_report()
            elif fmt == 'markdown':
                reporter.generate_markdown_report()
        
        # Generate fix script if not fixing
        if not args.fix:
            reporter.generate_fix_script()
        
        reporter._print_stage("All reports generated", "COMPLETED")
        
        # Print summary
        if args.print != PrintMode.QUIET:
            elapsed_time = time.time() - start_time
            print(f"\n⏱  Total time: {elapsed_time:.1f} seconds")
            print_summary(reporter)
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Analysis interrupted by user")
        return 130
    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return 1
    
    return return_code

if __name__ == '__main__':
    sys.exit(main())
