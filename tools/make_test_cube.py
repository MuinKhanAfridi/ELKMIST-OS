#!/usr/bin/env python3
"""make_test_cube.py OUT.stl [SIZE_MM] - write a small ASCII STL cube (used to self-test the slicer)."""
import sys
out = sys.argv[1]; s = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
v = [(0,0,0),(s,0,0),(s,s,0),(0,s,0),(0,0,s),(s,0,s),(s,s,s),(0,s,s)]
f = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
def n(a,b,c):
    u=[b[i]-a[i] for i in range(3)]; w=[c[i]-a[i] for i in range(3)]
    x=(u[1]*w[2]-u[2]*w[1], u[2]*w[0]-u[0]*w[2], u[0]*w[1]-u[1]*w[0])
    l=sum(t*t for t in x)**0.5 or 1
    return [t/l for t in x]
with open(out,"w") as fh:
    fh.write("solid cube\n")
    for a,b,c in f:
        A,B,C=v[a],v[b],v[c]; nn=n(A,B,C)
        fh.write("facet normal %f %f %f\n outer loop\n"%tuple(nn))
        for p in (A,B,C): fh.write("  vertex %f %f %f\n"%p)
        fh.write(" endloop\nendfacet\n")
    fh.write("endsolid cube\n")
