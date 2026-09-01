"""
YouTube video category ID to name mapping.
These are the standard YouTube category IDs.
"""

YOUTUBE_CATEGORIES = {
    1: "Film & Animation",
    2: "Autos & Vehicles",
    10: "Music",
    15: "Pets & Animals",
    17: "Sports",
    18: "Short Movies",
    19: "Travel & Events",
    20: "Gaming",
    21: "Videoblogging",
    22: "People & Blogs",
    23: "Comedy",
    24: "Entertainment",
    25: "News & Politics",
    26: "Howto & Style",
    27: "Education",
    28: "Science & Technology",
    29: "Nonprofits & Activism",
    30: "Movies",
    31: "Anime/Animation",
    32: "Action/Adventure",
    33: "Classics",
    34: "Comedy",
    35: "Documentary",
    36: "Drama",
    37: "Family",
    38: "Foreign",
    39: "Horror",
    40: "Sci-Fi/Fantasy",
    41: "Thriller",
    42: "Shorts",
    43: "Shows",
    44: "Trailers",
}


def get_category_name(category_id: int) -> str:
    """
    Get the category name for a given category ID.
    
    Args:
        category_id: YouTube category ID
        
    Returns:
        Category name string, or "Unknown" if not found
    """
    return YOUTUBE_CATEGORIES.get(category_id, "Unknown")


def get_category_id(category_name: str) -> int | None:
    """
    Get the category ID for a given category name.
    
    Args:
        category_name: Category name string
        
    Returns:
        Category ID, or None if not found
    """
    for cat_id, name in YOUTUBE_CATEGORIES.items():
        if name.lower() == category_name.lower():
            return cat_id
    return None

