# from numpy import log as ln
# import math 
# import random




def search(data,target,l,r, func=None):
	'''
	@function search - Performs a binary search on a sorted list 
		and returns the index of the found element. 
	@param data - list - The list to be passed in. 
	@param target - obj - the key that you are searching for.
	@param l - int - the left most index. 
	@param r - int -  the right most index.
	@param func - function - the look up function to 
		extract the sorting criteria out of custom objects. 
	
	example: 
		func = lambda list, index: list[index].getPID()
		search(tasks,pid,0,length,lambda data, func)

	@return - int - the index where the element should be added.
	'''
	if l < 0 or r > len(data): raise IndexError

	if func == None:
		func = lambda dat,index: dat[index]

	mid = ((r - l) / 2)
	mid = int(mid)
	mid += l

	if r >= l and mid < len(data):
		if func(data,mid) == target:
			return mid
		elif func(data,mid) > target:
			return search(data,target,l,mid - 1, func)
		else:
			return search(data,target,mid + 1,r, func)
	else:
		return -1


def insert(data,target,l,r,func=None):
	'''
	@function insert - Performs a binary search on a sorted list 
		and returns the index of where the element should be added. 
	@param data - list - The list to be passed in. 
	@param target - obj - the key that you are searching for.
	@param l - int - the left most index. 
	@param r - int -  the right most index.
	@param func - function - the look up function to 
		extract the sorting criteria out of custom objects. 
	
	example: 
		func = lambda list, index: list[index].getPID()
		insert(tasks,pid,0,length,lambda data, func)

	@return - int - the index where the element should be added.
	'''
	if l < 0 or r > len(data): raise IndexError
	
	if func == None:
		func = lambda dat,index: dat[index]

	mid = (r - l)/2
	mid = int(mid)
	mid += l
	if r >= l and mid < len(data):
		if func(data,mid) == target:
			return mid 
		elif func(data,mid) > target:
			return insert(data,target,l,mid - 1, func)
		else:
			return insert(data,target,mid + 1,r, func) 
	else:
		return l





# def round_up(n, decimals=0): 
# 	'''
# 	Rounds the value up
# 	Used For testing will be put in another file 
# 	'''
# 	multiplier = 10 ** decimals
# 	return math.ceil(n * multiplier) / multiplier


# def test_search():
# 	'''
# 	Used For testing will be put in another file 
# 	'''
# 	global count 
# 	flaws = list()
# 	for x in range(LENGTH):
# 		count = 0
# 		target = random.randint(0,LENGTH)
# 		print(search(data,target,0,len(data)))
# 		if count > MAXTIME: 
# 			flaws.append("Error with %s, count %s , max: %s " % (x, count, MAXTIME))
# 	print(search(data,-1,-1,len(data)))
# 	print(search(data,LENGTH,-1,len(data)))
# 	[print(x) for x in flaws]


# def insertNode(test_Insert,x):
# 	'''
# 	Used For testing will be put in another file 
# 	'''
# 	result = insert(test_Insert,x,0,len(test_Insert))
# 	test_Insert.insert(result,x)
# 	return  test_Insert


# def test_insert():
# 	'''
# 	Used For testing will be put in another file 
# 	'''
# 	toSort = [random.randint(-LENGTH,LENGTH) for x in range(LENGTH)]
# 	test_Insert = []
# 	print(toSort[1:10])
# 	for x in toSort:
# 		test_insert = insertNode(test_Insert,x)
# 	if test_Insert == sorted(toSort):
# 		print(test_insert[1:10])
# 		print('True')


# if __name__=='__main__':

# 	LENGTH = 2**12

# 	MAXTIME = ln(LENGTH)/ln(2)
# 	MAXTIME = round_up(MAXTIME) + 1

# 	print("Length %s Maxtime %s" % (LENGTH, MAXTIME))
# 	data = list(range(LENGTH))

# 	count = 0

# 	test_search()
# 	test_insert()