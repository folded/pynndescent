# Author: Leland McInnes <leland.mcinnes@gmail.com>
#
# License: BSD 2 clause

import time

import numba
from numba.core import types
import numba.experimental.structref as structref
import numpy as np


@numba.njit("void(i8[:], i8)")
def seed(rng_state, seed):
    """Seed the random number generator with a given seed."""
    rng_state.fill(seed + 0xFFFF)


@numba.njit("i4(i8[:])")
def tau_rand_int(state):
    """A fast (pseudo)-random number generator.

    Parameters
    ----------
    state: array of int64, shape (3,)
        The internal state of the rng

    Returns
    -------
    A (pseudo)-random int32 value
    """
    state[0] = (((state[0] & 4294967294) << 12) & 0xFFFFFFFF) ^ (
        (((state[0] << 13) & 0xFFFFFFFF) ^ state[0]) >> 19
    )
    state[1] = (((state[1] & 4294967288) << 4) & 0xFFFFFFFF) ^ (
        (((state[1] << 2) & 0xFFFFFFFF) ^ state[1]) >> 25
    )
    state[2] = (((state[2] & 4294967280) << 17) & 0xFFFFFFFF) ^ (
        (((state[2] << 3) & 0xFFFFFFFF) ^ state[2]) >> 11
    )

    return state[0] ^ state[1] ^ state[2]


@numba.njit("f4(i8[:])")
def tau_rand(state):
    """A fast (pseudo)-random number generator for floats in the range [0,1]

    Parameters
    ----------
    state: array of int64, shape (3,)
        The internal state of the rng

    Returns
    -------
    A (pseudo)-random float32 in the interval [0, 1]
    """
    integer = tau_rand_int(state)
    return abs(float(integer) / 0x7FFFFFFF)


@numba.njit(
    [
        "f4(f4[::1])",
        numba.types.float32(
            numba.types.Array(numba.types.float32, 1, "C", readonly=True)
        ),
    ],
    locals={
        "dim": numba.types.intp,
        "i": numba.types.uint32,
        "result": numba.types.float32,
    },
    fastmath=True,
)
def norm(vec):
    """Compute the (standard l2) norm of a vector.

    Parameters
    ----------
    vec: array of shape (dim,)

    Returns
    -------
    The l2 norm of vec.
    """
    result = 0.0
    dim = vec.shape[0]
    for i in range(dim):
        result += vec[i] * vec[i]
    return np.sqrt(result)


@numba.njit()
def rejection_sample(n_samples, pool_size, rng_state):
    """Generate n_samples many integers from 0 to pool_size such that no
    integer is selected twice. The duplication constraint is achieved via
    rejection sampling.

    Parameters
    ----------
    n_samples: int
        The number of random samples to select from the pool

    pool_size: int
        The size of the total pool of candidates to sample from

    rng_state: array of int64, shape (3,)
        Internal state of the random number generator

    Returns
    -------
    sample: array of shape(n_samples,)
        The ``n_samples`` randomly selected elements from the pool.
    """
    result = np.empty(n_samples, dtype=np.int64)
    for i in range(n_samples):
        reject_sample = True
        j = 0
        while reject_sample:
            j = tau_rand_int(rng_state) % pool_size
            for k in range(i):
                if j == result[k]:
                    break
            else:
                reject_sample = False
        result[i] = j
    return result


@structref.register
class HeapType(types.StructRef):
    pass


class Heap(structref.StructRefProxy):
    @property
    def indices(self):
        return Heap_get_indices(self)

    @property
    def distances(self):
        return Heap_get_distances(self)

    @property
    def flags(self):
        return Heap_get_flags(self)


@numba.njit
def Heap_get_flags(self):
    return self.flags


@numba.njit
def Heap_get_distances(self):
    return self.distances


@numba.njit
def Heap_get_indices(self):
    return self.indices


structref.define_proxy(
    Heap,
    HeapType,
    ["indices", "distances", "flags"],
)

# Heap = namedtuple("Heap", ("indices", "distances", "flags"))


@numba.njit()
def make_heap(n_points, size):
    """Constructor for the numba enabled heap objects. The heaps are used
    for approximate nearest neighbor search, maintaining a list of potential
    neighbors sorted by their distance. We also flag if potential neighbors
    are newly added to the list or not. Internally this is stored as
    a single ndarray; the first axis determines whether we are looking at the
    array of candidate graph_indices, the array of distances, or the flag array for
    whether elements are new or not. Each of these arrays are of shape
    (``n_points``, ``size``)

    Parameters
    ----------
    n_points: int
        The number of graph_data points to track in the heap.

    size: int
        The number of items to keep on the heap for each graph_data point.

    Returns
    -------
    heap: An ndarray suitable for passing to other numba enabled heap functions.
    """
    indices = np.full((int(n_points), int(size)), -1, dtype=np.int32)
    distances = np.full((int(n_points), int(size)), np.infty, dtype=np.float32)
    flags = np.zeros((int(n_points), int(size)), dtype=np.uint8)
    result = (indices, distances, flags)

    return result


@numba.jit(
    locals={
        "indices": numba.types.int32[::1],
        "weights": numba.types.float32[::1],
        "is_new": numba.types.uint8[::1],
        "i": numba.types.uint16,
        "ic1": numba.types.uint16,
        "ic2": numba.types.uint16,
        "i_swap": numba.types.uint16,
        "heap_size": numba.types.uint16,
    }
)
def heap_push(heap, row, weight, index, flag):
    """Push a new element onto the heap. The heap stores potential neighbors
    for each graph_data point. The ``row`` parameter determines which graph_data point we
    are addressing, the ``weight`` determines the distance (for heap sorting),
    the ``index`` is the element to add, and the flag determines whether this
    is to be considered a new addition.

    Parameters
    ----------
    heap: ndarray generated by ``make_heap``
        The heap object to push into

    row: int
        Which actual heap within the heap object to push to

    weight: float
        The priority value of the element to push onto the heap

    index: int
        The actual value to be pushed

    flag: int
        Whether to flag the newly added element or not.

    Returns
    -------
    success: The number of new elements successfully pushed into the heap.
    """
    row = np.int32(row)
    weight = np.float32(weight)
    index = np.int32(index)
    flag = np.uint8(flag)

    indices = heap[0][row]
    weights = heap[1][row]
    is_new = heap[2][row]

    if weight >= weights[0]:
        return 0

    # break if we already have this element.
    for i in range(indices.shape[0]):
        if index == indices[i]:
            return 0

    # insert val at position zero
    weights[0] = weight
    indices[0] = index
    is_new[0] = flag

    # descend the heap, swapping values until the max heap criterion is met
    i = 0
    while True:
        ic1 = 2 * i + 1
        ic2 = ic1 + 1

        if ic1 >= indices.shape[0]:
            break
        elif ic2 >= indices.shape[0]:
            if weights[ic1] > weight:
                i_swap = ic1
            else:
                break
        elif weights[ic1] >= weights[ic2]:
            if weight < weights[ic1]:
                i_swap = ic1
            else:
                break
        else:
            if weight < weights[ic2]:
                i_swap = ic2
            else:
                break

        weights[i] = weights[i_swap]
        indices[i] = indices[i_swap]
        is_new[i] = is_new[i_swap]

        i = i_swap

    weights[i] = weight
    indices[i] = index
    is_new[i] = flag

    return 1


@numba.jit(
    locals={
        "indices": numba.types.int32[::1],
        "weights": numba.types.float32[::1],
        "is_new": numba.types.uint8[::1],
        "i": numba.types.uint16,
        "ic1": numba.types.uint16,
        "ic2": numba.types.uint16,
        "i_swap": numba.types.uint16,
        "heap_size": numba.types.uint16,
    }
)
def unchecked_heap_push(heap, row, weight, index, flag):
    """Push a new element onto the heap. The heap stores potential neighbors
    for each graph_data point. The ``row`` parameter determines which graph_data point we
    are addressing, the ``weight`` determines the distance (for heap sorting),
    the ``index`` is the element to add, and the flag determines whether this
    is to be considered a new addition.

    Parameters
    ----------
    heap: ndarray generated by ``make_heap``
        The heap object to push into

    row: int
        Which actual heap within the heap object to push to

    weight: float
        The priority value of the element to push onto the heap

    index: int
        The actual value to be pushed

    flag: int
        Whether to flag the newly added element or not.

    Returns
    -------
    success: The number of new elements successfully pushed into the heap.
    """
    if weight >= heap[1][row, 0]:
        return 0

    indices = heap[0][row]
    weights = heap[1][row]
    is_new = heap[2][row]

    # insert val at position zero
    weights[0] = weight
    indices[0] = index
    is_new[0] = flag

    heap_size = indices.shape[0]

    # descend the heap, swapping values until the max heap criterion is met
    i = 0
    while True:
        ic1 = 2 * i + 1
        ic2 = ic1 + 1

        if ic1 >= heap_size:
            break
        elif ic2 >= heap_size:
            if weights[ic1] > weight:
                i_swap = ic1
            else:
                break
        elif weights[ic1] >= weights[ic2]:
            if weight < weights[ic1]:
                i_swap = ic1
            else:
                break
        else:
            if weight < weights[ic2]:
                i_swap = ic2
            else:
                break

        weights[i] = weights[i_swap]
        indices[i] = indices[i_swap]
        is_new[i] = is_new[i_swap]

        i = i_swap

    weights[i] = weight
    indices[i] = index
    is_new[i] = flag

    return 1


@numba.njit()
def siftdown(heap1, heap2, elt):
    """Restore the heap property for a heap with an out of place element
    at position ``elt``. This works with a heap pair where heap1 carries
    the weights and heap2 holds the corresponding elements."""
    while elt * 2 + 1 < heap1.shape[0]:
        left_child = elt * 2 + 1
        right_child = left_child + 1
        swap = elt

        if heap1[swap] < heap1[left_child]:
            swap = left_child

        if right_child < heap1.shape[0] and heap1[swap] < heap1[right_child]:
            swap = right_child

        if swap == elt:
            break
        else:
            heap1[elt], heap1[swap] = heap1[swap], heap1[elt]
            heap2[elt], heap2[swap] = heap2[swap], heap2[elt]
            elt = swap


@numba.njit()
def deheap_sort(heap):
    """Given an array of heaps (of graph_indices and weights), unpack the heap
    out to give and array of sorted lists of graph_indices and weights by increasing
    weight. This is effectively just the second half of heap sort (the first
    half not being required since we already have the graph_data in a heap).

    Parameters
    ----------
    heap : array of shape (3, n_samples, n_neighbors)
        The heap to turn into sorted lists.

    Returns
    -------
    graph_indices, weights: arrays of shape (n_samples, n_neighbors)
        The graph_indices and weights sorted by increasing weight.
    """
    indices = heap[0]
    weights = heap[1]

    for i in range(indices.shape[0]):

        ind_heap = indices[i]
        dist_heap = weights[i]

        for j in range(ind_heap.shape[0] - 1):
            ind_heap[0], ind_heap[ind_heap.shape[0] - j - 1] = (
                ind_heap[ind_heap.shape[0] - j - 1],
                ind_heap[0],
            )
            dist_heap[0], dist_heap[dist_heap.shape[0] - j - 1] = (
                dist_heap[dist_heap.shape[0] - j - 1],
                dist_heap[0],
            )

            siftdown(
                dist_heap[: dist_heap.shape[0] - j - 1],
                ind_heap[: ind_heap.shape[0] - j - 1],
                0,
            )

    return indices.astype(np.int64), weights


@numba.njit()
def smallest_flagged(heap, row):
    """Search the heap for the smallest element that is
    still flagged.

    Parameters
    ----------
    heap: array of shape (3, n_samples, n_neighbors)
        The heaps to search

    row: int
        Which of the heaps to search

    Returns
    -------
    index: int
        The index of the smallest flagged element
        of the ``row``th heap, or -1 if no flagged
        elements remain in the heap.
    """
    ind = heap[0][row]
    dist = heap[1][row]
    flag = heap[2][row]

    min_dist = np.inf
    result_index = -1

    for i in range(ind.shape[0]):
        if flag[i] == 1 and dist[i] < min_dist:
            min_dist = dist[i]
            result_index = i

    if result_index >= 0:
        flag[result_index] = 0.0
        return int(ind[result_index])
    else:
        return -1


@numba.njit(parallel=True, locals={"idx": numba.types.int64})
def new_build_candidates(
    current_graph,
    max_candidates,
    rng_state,
):
    """Build a heap of candidate neighbors for nearest neighbor descent. For
    each vertex the candidate neighbors are any current neighbors, and any
    vertices that have the vertex as one of their nearest neighbors.

    Parameters
    ----------
    current_graph: heap
        The current state of the graph for nearest neighbor descent.

    max_candidates: int
        The maximum number of new candidate neighbors.

    rng_state: array of int64, shape (3,)
        The internal state of the rng

    Returns
    -------
    candidate_neighbors: A heap with an array of (randomly sorted) candidate
    neighbors for each vertex in the graph.
    """
    current_indices = current_graph[0]
    current_flags = current_graph[2]

    n_vertices = current_indices.shape[0]
    n_neighbors = current_indices.shape[1]

    new_candidate_indices = np.full((n_vertices, max_candidates), -1, dtype=np.int32)
    new_candidate_priority = np.full(
        (n_vertices, max_candidates), np.inf, dtype=np.float32
    )

    old_candidate_indices = np.full((n_vertices, max_candidates), -1, dtype=np.int32)
    old_candidate_priority = np.full(
        (n_vertices, max_candidates), np.inf, dtype=np.float32
    )

    n_threads = numba.get_num_threads()

    for n in numba.prange(n_threads):
        local_rng_state = rng_state + n
        for i in range(n_vertices):
            for j in range(n_neighbors):
                idx = current_indices[i, j]
                isn = current_flags[i, j]

                if idx < 0:
                    continue

                d = tau_rand(local_rng_state)

                if isn:
                    if i % n_threads == n:
                        checked_heap_push(
                            new_candidate_priority[i],
                            new_candidate_indices[i],
                            d,
                            idx,
                        )
                    if idx % n_threads == n:
                        checked_heap_push(
                            new_candidate_priority[idx],
                            new_candidate_indices[idx],
                            d,
                            i,
                        )
                else:
                    if i % n_threads == n:
                        checked_heap_push(
                            old_candidate_priority[i],
                            old_candidate_indices[i],
                            d,
                            idx,
                        )
                    if idx % n_threads == n:
                        checked_heap_push(
                            old_candidate_priority[idx],
                            old_candidate_indices[idx],
                            d,
                            i,
                        )

    indices = current_graph[0]
    flags = current_graph[2]

    for i in numba.prange(n_vertices):
        for j in range(n_neighbors):
            idx = indices[i, j]

            for k in range(max_candidates):
                if new_candidate_indices[i, k] == idx:
                    flags[i, j] = 0
                    break

    return new_candidate_indices, old_candidate_indices


@numba.njit("b1(u1[::1],i4)")
def has_been_visited(table, candidate):
    loc = candidate >> 3
    mask = 1 << (candidate & 7)
    return table[loc] & mask


@numba.njit("void(u1[::1],i4)")
def mark_visited(table, candidate):
    loc = candidate >> 3
    mask = 1 << (candidate & 7)
    table[loc] |= mask
    return


@numba.njit(
    "i4(f4[::1],i4[::1],f4,i4)",
    fastmath=True,
    locals={
        "size": numba.types.intp,
        "i": numba.types.uint16,
        "ic1": numba.types.uint16,
        "ic2": numba.types.uint16,
        "i_swap": numba.types.uint16,
    },
)
def simple_heap_push(priorities, indices, p, n):
    if p >= priorities[0]:
        return 0

    size = priorities.shape[0]

    # insert val at position zero
    priorities[0] = p
    indices[0] = n

    # descend the heap, swapping values until the max heap criterion is met
    i = 0
    while True:
        ic1 = 2 * i + 1
        ic2 = ic1 + 1

        if ic1 >= size:
            break
        elif ic2 >= size:
            if priorities[ic1] > p:
                i_swap = ic1
            else:
                break
        elif priorities[ic1] >= priorities[ic2]:
            if p < priorities[ic1]:
                i_swap = ic1
            else:
                break
        else:
            if p < priorities[ic2]:
                i_swap = ic2
            else:
                break

        priorities[i] = priorities[i_swap]
        indices[i] = indices[i_swap]

        i = i_swap

    priorities[i] = p
    indices[i] = n

    return 1


@numba.njit(
    "i4(f4[::1],i4[::1],f4,i4)",
    fastmath=True,
    locals={
        "size": numba.types.intp,
        "i": numba.types.uint16,
        "ic1": numba.types.uint16,
        "ic2": numba.types.uint16,
        "i_swap": numba.types.uint16,
    },
)
def checked_heap_push(priorities, indices, p, n):
    if p >= priorities[0]:
        return 0

    size = priorities.shape[0]

    # break if we already have this element.
    for i in range(size):
        if n == indices[i]:
            return 0

    # insert val at position zero
    priorities[0] = p
    indices[0] = n

    # descend the heap, swapping values until the max heap criterion is met
    i = 0
    while True:
        ic1 = 2 * i + 1
        ic2 = ic1 + 1

        if ic1 >= size:
            break
        elif ic2 >= size:
            if priorities[ic1] > p:
                i_swap = ic1
            else:
                break
        elif priorities[ic1] >= priorities[ic2]:
            if p < priorities[ic1]:
                i_swap = ic1
            else:
                break
        else:
            if p < priorities[ic2]:
                i_swap = ic2
            else:
                break

        priorities[i] = priorities[i_swap]
        indices[i] = indices[i_swap]

        i = i_swap

    priorities[i] = p
    indices[i] = n

    return 1


@numba.njit(
    "i4(f4[::1],i4[::1],u1[::1],f4,i4,u1)",
    fastmath=True,
    locals={
        "size": numba.types.intp,
        "i": numba.types.uint16,
        "ic1": numba.types.uint16,
        "ic2": numba.types.uint16,
        "i_swap": numba.types.uint16,
    },
)
def flagged_heap_push(priorities, indices, flags, p, n, f):
    if p >= priorities[0]:
        return 0

    size = priorities.shape[0]

    # insert val at position zero
    priorities[0] = p
    indices[0] = n
    flags[0] = f

    # descend the heap, swapping values until the max heap criterion is met
    i = 0
    while True:
        ic1 = 2 * i + 1
        ic2 = ic1 + 1

        if ic1 >= size:
            break
        elif ic2 >= size:
            if priorities[ic1] > p:
                i_swap = ic1
            else:
                break
        elif priorities[ic1] >= priorities[ic2]:
            if p < priorities[ic1]:
                i_swap = ic1
            else:
                break
        else:
            if p < priorities[ic2]:
                i_swap = ic2
            else:
                break

        priorities[i] = priorities[i_swap]
        indices[i] = indices[i_swap]
        flags[i] = flags[i_swap]

        i = i_swap

    priorities[i] = p
    indices[i] = n
    flags[i] = f

    return 1


@numba.njit(
    "i4(f4[::1],i4[::1],u1[::1],f4,i4,u1)",
    fastmath=True,
    locals={
        "size": numba.types.intp,
        "i": numba.types.uint16,
        "ic1": numba.types.uint16,
        "ic2": numba.types.uint16,
        "i_swap": numba.types.uint16,
    },
)
def checked_flagged_heap_push(priorities, indices, flags, p, n, f):
    if p >= priorities[0]:
        return 0

    size = priorities.shape[0]

    # break if we already have this element.
    for i in range(size):
        if n == indices[i]:
            return 0

    # insert val at position zero
    priorities[0] = p
    indices[0] = n
    flags[0] = f

    # descend the heap, swapping values until the max heap criterion is met
    i = 0
    while True:
        ic1 = 2 * i + 1
        ic2 = ic1 + 1

        if ic1 >= size:
            break
        elif ic2 >= size:
            if priorities[ic1] > p:
                i_swap = ic1
            else:
                break
        elif priorities[ic1] >= priorities[ic2]:
            if p < priorities[ic1]:
                i_swap = ic1
            else:
                break
        else:
            if p < priorities[ic2]:
                i_swap = ic2
            else:
                break

        priorities[i] = priorities[i_swap]
        indices[i] = indices[i_swap]
        flags[i] = flags[i_swap]

        i = i_swap

    priorities[i] = p
    indices[i] = n
    flags[i] = f

    return 1


@numba.njit(
    parallel=True,
    locals={
        "p": numba.int32,
        "q": numba.int32,
        "d": numba.float32,
        "added": numba.uint8,
        "n": numba.uint32,
        "i": numba.uint32,
        "j": numba.uint32,
    },
)
def apply_graph_updates_low_memory(current_graph, updates):

    n_changes = 0
    priorities = current_graph[1]
    indices = current_graph[0]
    flags = current_graph[2]
    n_threads = numba.get_num_threads()

    for n in numba.prange(n_threads):
        for i in range(len(updates)):
            for j in range(len(updates[i])):
                p, q, d = updates[i][j]

                if p == -1 or q == -1:
                    continue

                if p % n_threads == n:
                    # added = heap_push(current_graph, p, d, q, 1)
                    added = checked_flagged_heap_push(
                        priorities[p],
                        indices[p],
                        flags[p],
                        d,
                        q,
                        1,
                    )
                    n_changes += added

                if q % n_threads == n:
                    # added = heap_push(current_graph, q, d, p, 1)
                    added = checked_flagged_heap_push(
                        priorities[q],
                        indices[q],
                        flags[q],
                        d,
                        p,
                        1,
                    )
                    n_changes += added

    return n_changes


@numba.njit(locals={"p": numba.types.int64, "q": numba.types.int64})
def apply_graph_updates_high_memory(current_graph, updates, in_graph):

    n_changes = 0

    for i in range(len(updates)):
        for j in range(len(updates[i])):
            p, q, d = updates[i][j]

            if p == -1 or q == -1:
                continue

            if q in in_graph[p] and p in in_graph[q]:
                continue
            elif q in in_graph[p]:
                pass
            else:
                # added = unchecked_heap_push(current_graph, p, d, q, 1)
                added = flagged_heap_push(
                    current_graph[1][p],
                    current_graph[0][p],
                    current_graph[2][p],
                    d,
                    q,
                    1,
                )

                if added > 0:
                    in_graph[p].add(q)
                    n_changes += added

            if p == q or p in in_graph[q]:
                pass
            else:
                # added = unchecked_heap_push(current_graph, q, d, p, 1)
                added = flagged_heap_push(
                    current_graph[1][p],
                    current_graph[0][p],
                    current_graph[2][p],
                    d,
                    q,
                    1,
                )

                if added > 0:
                    in_graph[q].add(p)
                    n_changes += added

    return n_changes


@numba.njit()
def initalize_heap_from_graph_indices(heap, graph_indices, data, metric):

    for i in range(graph_indices.shape[0]):
        for idx in range(graph_indices.shape[1]):
            j = graph_indices[i, idx]
            if j >= 0:
                d = metric(data[i], data[j])
                flagged_heap_push(heap[1][i], heap[0][i], heap[2][i], d, j, 1)

    return heap


@numba.njit(parallel=True)
def sparse_initalize_heap_from_graph_indices(
    heap, graph_indices, data_indptr, data_indices, data_vals, metric
):

    for i in numba.prange(graph_indices.shape[0]):
        for idx in range(graph_indices.shape[1]):
            j = graph_indices[i, idx]
            ind1 = data_indices[data_indptr[i] : data_indptr[i + 1]]
            data1 = data_vals[data_indptr[i] : data_indptr[i + 1]]
            ind2 = data_indices[data_indptr[j] : data_indptr[j + 1]]
            data2 = data_vals[data_indptr[j] : data_indptr[j + 1]]
            d = metric(ind1, data1, ind2, data2)
            # unchecked_heap_push(heap, i, d, j, 1)
            flagged_heap_push(heap[0][i], heap[1][i], heap[2][i], j, d, 1)

    return heap


# Generates a timestamp for use in logging messages when verbose=True
def ts():
    return time.ctime(time.time())
