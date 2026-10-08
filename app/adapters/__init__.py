from .byxt import ByxtAdapter, CourseReminderAdapter
from .bykc import BykcAdapter
from .cgyy import CgyyAdapter
from .grade import GradeAdapter
from .iclass import IClassAdapter
from .judge import JudgeAdapter
from .libbook import LibBookAdapter
from .spoc import SpocAdapter
from .ygdk import YgdkAdapter

__all__ = [
    "BykcAdapter", "ByxtAdapter", "CourseReminderAdapter", "CgyyAdapter", "GradeAdapter", "IClassAdapter", "JudgeAdapter", "LibBookAdapter",
    "SpocAdapter", "YgdkAdapter",
]
