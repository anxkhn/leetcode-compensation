import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator

import requests  # type: ignore

from leetcomp.errors import FetchContentException, FetchPostsException
from leetcomp.queries import COMP_POST_CONTENT_DATA_QUERY as content_query
from leetcomp.queries import COMP_POSTS_DATA_QUERY as posts_query
from leetcomp.queries import COMP_POSTS_DATA_QUERY_LEGACY as posts_query_legacy
from leetcomp.utils import (
    config,
    latest_parsed_date,
    retry_with_exp_backoff,
    sort_and_truncate,
)


@dataclass
class LeetCodePost:
    id: str
    title: str
    content: str
    vote_count: int
    comment_count: int
    view_count: int
    creation_date: datetime


def get_posts_query(skip: int, first: int) -> dict[Any, Any]:
    query = posts_query.copy()
    query["variables"]["skip"] = skip  # type: ignore
    query["variables"]["first"] = first  # type: ignore
    return query


def get_posts_query_legacy(skip: int, first: int) -> dict[Any, Any]:
    query = posts_query_legacy.copy()
    query["variables"]["skip"] = skip  # type: ignore
    query["variables"]["first"] = first  # type: ignore
    return query


def get_content_query(post_id: int) -> dict[Any, Any]:
    query = content_query.copy()
    query["variables"]["topicId"] = post_id  # type: ignore
    return query


@retry_with_exp_backoff(retries=config["app"]["n_api_retries"])  # type: ignore
def post_content(post_id: int) -> str:
    query = get_content_query(post_id)
    response = requests.post(config["app"]["leetcode_graphql_url"], json=query)

    if response.status_code != 200:
        raise FetchContentException(
            f"Failed to fetch content for post_id={post_id}): {response.text}"
        )

    data = response.json().get("data")
    if not data:
        raise FetchContentException(
            f"Invalid response data for post_id={post_id}"
        )

    # Check if topic exists and has a post
    topic = data.get("topic")
    if not topic:
        raise FetchContentException(
            f"Topic not found for post_id={post_id}"
        )
    
    post = topic.get("post")
    if not post:
        raise FetchContentException(
            f"Post content not available for post_id={post_id} (post may be deleted or private)"
        )
    
    content = post.get("content")
    if content is None:
        raise FetchContentException(
            f"Post content is null for post_id={post_id}"
        )

    return str(content)


@retry_with_exp_backoff(retries=config["app"]["n_api_retries"])  # type: ignore
def parsed_posts(skip: int, first: int) -> Iterator[LeetCodePost]:
    """parse posts using new ugcArticleDiscussionArticles api"""
    query = get_posts_query(skip, first)
    response = requests.post(config["app"]["leetcode_graphql_url"], json=query)

    if response.status_code != 200:
        raise FetchPostsException(
            f"Failed to fetch content for skip={skip}, first={first}): {response.text}"
        )

    response_json = response.json()
    data = response_json.get("data")
    if not data:
        raise FetchPostsException(
            f"Invalid response data for skip={skip}, first={first}"
        )

    posts_data = data.get("ugcArticleDiscussionArticles")
    if not posts_data:
        raise FetchPostsException(
            f"No ugcArticleDiscussionArticles found in response for skip={skip}, first={first}"
        )
    
    posts = posts_data.get("edges", [])

    for post in posts:
        node = post["node"]
        
        vote_count = 0
        for reaction in node.get("reactions", []):
            if reaction["reactionType"] == "UPVOTE":
                vote_count = reaction["count"]
                break
        
        created_at = node["createdAt"]
        creation_date = datetime.fromisoformat(created_at.replace("+00:00", ""))
        content = node.get("summary", "")
        
        yield LeetCodePost(
            id=str(node["topicId"]),
            title=node["title"],
            content=content,
            vote_count=vote_count,
            comment_count=node.get("topic", {}).get("topLevelCommentCount", 0),
            view_count=node.get("hitCount", 0),
            creation_date=creation_date,
        )


@retry_with_exp_backoff(retries=config["app"]["n_api_retries"])  # type: ignore
def parsed_posts_legacy(skip: int, first: int) -> Iterator[LeetCodePost]:
    """parse posts using legacy categoryTopicList api"""
    query = get_posts_query_legacy(skip, first)
    response = requests.post(config["app"]["leetcode_graphql_url"], json=query)

    if response.status_code != 200:
        raise FetchPostsException(
            f"Failed to fetch content for skip={skip}, first={first}): {response.text}"
        )

    data = response.json().get("data")
    if not data:
        raise FetchPostsException(
            f"Invalid response data for skip={skip}, first={first}"
        )

    posts = data["categoryTopicList"]["edges"]

    if skip == 0:
        posts = posts[1:]  # Skip pinned post

    for post in posts:
        try:
            content = str(post_content(post["node"]["id"]))
        except FetchContentException as e:
            print(f"Warning: Skipping post {post['node']['id']} - {e}")
            continue
        
        yield LeetCodePost(
            id=post["node"]["id"],
            title=post["node"]["title"],
            content=content,
            vote_count=post["node"]["post"]["voteCount"],
            comment_count=post["node"]["commentCount"],
            view_count=post["node"]["viewCount"],
            creation_date=datetime.fromtimestamp(
                post["node"]["post"]["creationDate"]
            ),
        )


def parsed_posts(skip: int, first: int, cutoff_date: datetime) -> Iterator[LeetCodePost]:
    """automatically switch between apis based on date"""
    march_2025 = datetime(2025, 3, 1)
    
    if cutoff_date > march_2025:
        try:
            return parsed_posts(skip, first)
        except FetchPostsException as e:
            print(f"new api failed, falling back to legacy: {e}")
            return parsed_posts_legacy(skip, first)
    else:
        return parsed_posts_legacy(skip, first)


def get_latest_posts(
    comps_path: str, start_date: datetime, till_date: datetime
) -> None:
    skip, first = 0, 50
    has_crossed_till_date = False
    fetched_posts, skips_due_to_lag = 0, 0

    with open(comps_path, "a") as f:
        while not has_crossed_till_date:
            post_count_in_batch = 0
            
            for post in parsed_posts(skip, first, start_date):  # type: ignore[unused-ignore]
                post_count_in_batch += 1
                
                if post.creation_date > start_date:
                    skips_due_to_lag += 1
                    continue

                if post.creation_date <= till_date:
                    has_crossed_till_date = True
                    break

                post_dict = asdict(post)
                post_dict["creation_date"] = post.creation_date.strftime(
                    config["app"]["date_fmt"]
                )
                f.write(json.dumps(post_dict) + "\n")
                fetched_posts += 1

                if fetched_posts % 10 == 0:
                    print(
                        f"{post.creation_date} Fetched {fetched_posts} posts..."
                    )

            if post_count_in_batch == 0:
                break
                
            skip += first

        print(f"Skipped {skips_due_to_lag} posts due to lag...")
        print(f"{post.creation_date} Fetched {fetched_posts} posts in total!")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch latest posts from LeetCode Compensations page."
    )
    parser.add_argument(
        "--comps_path",
        type=str,
        default=config["app"]["data_dir"] / "raw_comps.jsonl",
        help="Path to the file to store posts.",
    )
    parser.add_argument(
        "--till_date",
        type=str,
        default="",
        help="Fetch posts till this date (YYYY/MM/DD).",
    )
    args = parser.parse_args()

    if not args.till_date:
        till_date = latest_parsed_date(args.comps_path)
    else:
        till_date = datetime.strptime(args.till_date, "%Y/%m/%d")

    print(f"Fetching posts till {till_date}...")

    start_date = datetime.now() - timedelta(days=config["app"]["lag_days"])
    get_latest_posts(args.comps_path, start_date, till_date)
    sort_and_truncate(args.comps_path, truncate=True)
