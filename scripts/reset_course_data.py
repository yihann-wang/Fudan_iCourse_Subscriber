"""Reset course data in the database.

Usage:
    python scripts/reset_course_data.py --course-id 30004
    python scripts/reset_course_data.py --course-id 30004,30005
    python scripts/reset_course_data.py --course-id 30004 --sub-title "2026-03-03第1-3节,2026-03-10第1-3节"
    python scripts/reset_course_data.py --course-id 30004,30005 --all

Options:
    --course-id   Comma-separated course IDs to reset (required).
    --sub-title   Comma-separated lecture titles to delete.
    --all         Delete ALL lectures for the specified courses.
    Without --sub-title or --all, lists lectures and exits.
"""

import argparse
import os
import sqlite3
import sys


def show_lectures(conn, course_id):
    """Print lecture list for a course. Returns the rows."""
    course = conn.execute(
        "SELECT * FROM courses WHERE course_id = ?", (course_id,)
    ).fetchone()
    if not course:
        print(f"\n  Course {course_id} not found in database.")
        return []

    print(f"\n  Course {course_id}: {course['title']} (Teacher: {course['teacher']})")

    lectures = conn.execute(
        "SELECT sub_id, sub_title, date, processed_at, emailed_at,"
        " error_stage, error_count FROM lectures WHERE course_id = ?",
        (course_id,),
    ).fetchall()

    if not lectures:
        print("  No lectures found.")
        return []

    for lec in lectures:
        status = []
        if lec["processed_at"]:
            status.append("processed")
        if lec["emailed_at"]:
            status.append("emailed")
        if lec["error_stage"]:
            status.append(f"error:{lec['error_stage']}(x{lec['error_count']})")
        status_str = ", ".join(status) if status else "pending"
        print(f"    [{lec['sub_id']}] {lec['sub_title']} ({lec['date']}) — {status_str}")

    return lectures


def main():
    parser = argparse.ArgumentParser(description="Reset course data in the database.")
    parser.add_argument("--course-id", required=True,
                        help="Comma-separated course ID(s) to reset")
    parser.add_argument("--sub-title", default=None,
                        help="Comma-separated lecture title(s) to delete")
    parser.add_argument("--all", action="store_true",
                        help="Delete ALL lectures for the specified courses")
    parser.add_argument("--db", default="data/icourse.db",
                        help="Database path (default: data/icourse.db)")
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    course_ids = [c.strip() for c in args.course_id.split(",") if c.strip()]
    sub_titles = (
        [s.strip() for s in args.sub_title.split(",") if s.strip()]
        if args.sub_title else []
    )

    # Show current state
    print("Current database state:")
    for cid in course_ids:
        show_lectures(conn, cid)

    # If neither --all nor --sub-title, just list and exit
    if not args.all and not sub_titles:
        print("\nUse --all to delete all lectures, or --sub-title to delete specific ones.")
        sys.exit(0)

    # Perform deletion
    total_deleted = 0
    with conn:
        for cid in course_ids:
            if args.all:
                count = conn.execute(
                    "DELETE FROM lectures WHERE course_id = ?", (cid,)
                ).rowcount
                total_deleted += count
                print(f"\nDeleted {count} lecture(s) for course {cid}.")
            else:
                for title in sub_titles:
                    count = conn.execute(
                        "DELETE FROM lectures WHERE course_id = ? AND sub_title = ?",
                        (cid, title),
                    ).rowcount
                    total_deleted += count
                    if count:
                        print(f"\nDeleted: [{cid}] '{title}' ({count} row(s))")
                    else:
                        print(f"\nNot found: [{cid}] '{title}'")

    print(f"\nTotal deleted: {total_deleted} row(s).")
    conn.close()


if __name__ == "__main__":
    main()
