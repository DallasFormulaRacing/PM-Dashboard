// main container holding general app layout

import React from 'react';
import {Outlet} from 'react-router-dom';
import Sidebar from './components/Sidebar.jsx';

export default function App() {
    return (
        <div className="sidecontainer">
            <Sidebar />
            <main className="maincontent">
                <Outlet />
            </main>
        </div>
    );
}