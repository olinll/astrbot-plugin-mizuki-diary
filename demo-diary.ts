// 日记数据配置
// 用于管理日记页面的数据

export interface DiaryItem {
	id: number;
	content: string;
	date: string;
	images?: string[];
	location?: string;
	mood?: string;
	tags?: string[];
}

// 示例日记数据
const diaryData: DiaryItem[] = [
	{
		id: 1,
		content:
			"不是都说成都是0最多的地方吗？\n但是我看未必，其实给最多的地方是英国。\n因为Sun Bro 帝国",
		date: "2026-03-29 21:20:00",
		images: ["/images/diary/omgl.webp"],
		location: "家中",
		mood: "OMG",
		tags: ["扯淡"]
	},

	{
		id: 2,
		content:
			"架构是冷的，但折腾的心是热的。",
		date: "2026-03-30 21:35:42",
		location: "无锡",
	},
	{
		id: 3,
		content:
			"为什么有些人喜欢听纯音乐？\n因为纯音乐的优点是，你不会被歌词左右。\n纯音乐最大的的缺点是，你会被回忆左右”",
		date: "2026-04-01 22:27:16",
		location: "无锡",
	},
];

// 获取日记列表（按时间倒序）
export const getDiaryList = (limit?: number) => {
	const sortedData = [...diaryData].sort(
		(a, b) => new Date(b.date).getTime() - new Date(a.date).getTime(),
	);

	if (limit && limit > 0) {
		return sortedData.slice(0, limit);
	}

	return sortedData;
};

// 获取所有标签
export const getAllTags = () => {
	const tags = new Set<string>();
	diaryData.forEach((item) => {
		if (item.tags) {
			item.tags.forEach((tag) => tags.add(tag));
		}
	});
	return Array.from(tags).sort();
};