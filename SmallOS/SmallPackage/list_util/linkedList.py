
'''
@file linkedList - modules to create and manipulate doubly linked list. 
'''

def insertPrev(root, newNode):
    '''
    @function insertPrev() - takes in a rootNode and newNode and 
        inserts the newNode behind the rootNode.
    @param root -  Node() - a doubly-linked-list node that the 
        newNode is added behind. 
    @param newNode - Node() - a doubly-linked-list node that is 
        being added behind the root.
    @return void
    '''
    temp = root.prev 

    root.prev = newNode
    newNode.prev = temp

    newNode.next = root
    if temp:
        temp.next = newNode
    return 


def insertNext(root,newNode):
    '''
    @function insertNext() - takes in a rootNode and newNode and 
        inserts the newNode infront of the rootNode.
    @param root -  Node() - a doubly-linked-list node that the 
        newNode is added infront of. 
    @param newNode - Node() - a doubly-linked-list node that is 
        being added in front of the root.
    @return void
    '''
    temp = root.next

    root.next = newNode
    newNode.next = temp

    newNode.prev = root
    if temp:
        temp.prev = newNode
    return


def removeNode(node):
    '''
    @function removeNode() - Removes the Node() from the 
        doubly-linked-list
    @param node -  Node() - Node to be removed from the list.
    @return void
    ***NOTE
        The node is not deleted from memory but only 
        from the list. ***
    '''
    prev = node.prev
    next = node.next 

    if prev:
        prev.next = next
    if next:
        next.prev = prev
    return 




class Node():
    '''
    @class Node() - Holds no data on its own, 
        developed with the intent of being a super class
        and being inherited. 
        
        ***NOTE
        Whatever inherits this class will be given the support
        for this data so long as the nametags next and prev are
        are not taken. 
        ***
    '''
    next=None
    prev=None

    def __init__(self):
        self.next = None
        self.prev = None 
        return 



